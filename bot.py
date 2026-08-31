import aiosqlite
import asyncio
import logging
import os
import sqlite3
from aiohttp import web
from contextlib import asynccontextmanager
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import TelegramError

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
UNLOCK_DELAY = float(os.getenv("UNLOCK_DELAY", 7.5))
ADMIN_ID = int(os.getenv("ADMIN_ID", 8984398175))

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "bot_data.db"
DB_TIMEOUT = 60.0

# In-Memory Cache for Zero-Disk-Read Config Lookup
CONFIG_CACHE = {
    "protected": None,
    "unprotected": None,
    "delay": None
}

@asynccontextmanager
async def get_db():
    """Async context manager for aiosqlite configured with WAL mode and high busy_timeout."""
    async with aiosqlite.connect(DB_FILE, timeout=DB_TIMEOUT) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=60000;")
        yield conn

async def load_config_cache():
    """Load configuration values from DB into memory cache."""
    try:
        async with get_db() as conn:
            async with conn.execute("SELECT key, value FROM config") as cursor:
                rows = await cursor.fetchall()
                for key, val in rows:
                    CONFIG_CACHE[key] = val
        logger.info(f"Loaded config cache into memory: {CONFIG_CACHE}")
    except Exception as e:
        logger.error(f"Failed to load config cache: {e}")

async def init_db():
    async with get_db() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_jobs (
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                unlock_time REAL,
                PRIMARY KEY (user_id, chat_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cleanup_jobs (
                chat_id INTEGER,
                message_id INTEGER,
                delete_time REAL,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                joined_at REAL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await conn.commit()

async def delete_unprotected_task(context: ContextTypes.DEFAULT_TYPE):
    """Deletes messages automatically (used for user /start message and unprotected message)."""
    job = context.job
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]

    logger.info(f"Running auto-delete task for message {message_id} in chat {chat_id}")

    for attempt in range(5):
        try:
            async with get_db() as conn:
                await conn.execute("DELETE FROM cleanup_jobs WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
                await conn.commit()
            break
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))
            else:
                logger.error(f"Failed to remove cleanup job from DB: {e}")

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Message {message_id} in chat {chat_id} deleted automatically.")
    except TelegramError as e:
        logger.warning(f"Failed to delete message {message_id} in chat {chat_id}: {e}")

async def unlock_task(context: ContextTypes.DEFAULT_TYPE):
    """The task that runs after delay to delete the protected message and send the unprotected one."""
    job = context.job
    user_id = job.data["user_id"]
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]

    logger.info(f"Running unlock task for user {user_id} in chat {chat_id}")

    for attempt in range(5):
        try:
            async with get_db() as conn:
                await conn.execute("DELETE FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
                await conn.commit()
            break
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))
            else:
                logger.error(f"Failed to remove job from DB: {e}")

    # 1. Delete protected message
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Protected message {message_id} deleted successfully.")
    except TelegramError as e:
        logger.warning(f"Failed to delete message {message_id}: {e}")
    
    # 2. Send unprotected message (from RAM cache)
    unprotected_text = CONFIG_CACHE.get("unprotected") or "Thanks for the cooperation, now forward this message as you wish."
        
    try:
        sent_unprotected = await context.bot.send_message(
            chat_id=chat_id,
            text=unprotected_text,
            protect_content=False
        )
        logger.info(f"Unprotected message sent to {chat_id}.")

        # Schedule auto-deletion of unprotected message after 2 minutes (120 seconds)
        delete_delay = 120.0
        delete_time = datetime.now().timestamp() + delete_delay
        
        for attempt in range(5):
            try:
                async with get_db() as conn:
                    await conn.execute(
                        "INSERT OR REPLACE INTO cleanup_jobs (chat_id, message_id, delete_time) VALUES (?, ?, ?)",
                        (chat_id, sent_unprotected.message_id, delete_time)
                    )
                    await conn.commit()
                break
            except Exception as e:
                if attempt < 4:
                    await asyncio.sleep(0.1 * (attempt + 1))
                else:
                    logger.error(f"Failed to record cleanup job in DB: {e}")
            
        cleanup_job_data = {
            "chat_id": chat_id,
            "message_id": sent_unprotected.message_id
        }
        context.job_queue.run_once(
            delete_unprotected_task,
            delete_delay,
            data=cleanup_job_data,
            name=f"delete_unprotected_{chat_id}_{sent_unprotected.message_id}"
        )
        logger.info(f"Scheduled 2-minute auto-deletion for unprotected message {sent_unprotected.message_id} in chat {chat_id}.")
    except TelegramError as e:
        logger.error(f"Failed to send unprotected message to {chat_id}: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command with WAL mode, busy timeout, retries, and high concurrency resilience."""
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or not chat:
        return
    
    now = datetime.now().timestamp()
    logger.info(f"User {user.id} started the bot in chat {chat.id}")

    # Step 1: Check active job & register user in DB (with retry logic)
    has_active_job = False
    for attempt in range(3):
        try:
            async with get_db() as conn:
                await conn.execute("INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?, ?)", (user.id, now))
                async with conn.execute("SELECT unlock_time FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user.id, chat.id)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        has_active_job = True
                await conn.commit()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                await asyncio.sleep(0.1 * (attempt + 1))
            else:
                logger.warning(f"DB locked when checking active job for user {user.id}: {e}")
        except Exception as e:
            logger.error(f"Unexpected DB error checking active job: {e}")
            break

    if has_active_job:
        logger.info(f"User {user.id} already has a pending job. Ignoring duplicate start.")
        return

    # Step 2: Fetch protected text & timer delay from memory cache (Zero Disk Read)
    protected_text = CONFIG_CACHE.get("protected") or (
        "To forward this message tap this link-\n\n"
        "https://t.me/sae_plays/3 (stay for 5-10 sec)"
    )

    raw_delay = CONFIG_CACHE.get("delay")
    if raw_delay:
        try:
            current_delay = float(raw_delay)
        except ValueError:
            current_delay = UNLOCK_DELAY
    else:
        current_delay = UNLOCK_DELAY

    unlock_time = now + current_delay
    start_delete_delay = 30.0
    start_delete_time = now + start_delete_delay

    # Step 3: Send protected message (GUARANTEED delivery to user!)
    try:
        sent_message = await context.bot.send_message(
            chat_id=chat.id,
            text=protected_text,
            protect_content=True
        )
        logger.info("Protected message sent.")
    except TelegramError as e:
        logger.error(f"Failed to send protected message to {chat.id}: {e}")
        return

    # Step 4: Record jobs in DB (with retry logic)
    for attempt in range(5):
        try:
            async with get_db() as conn:
                await conn.execute(
                    "INSERT INTO active_jobs (user_id, chat_id, message_id, unlock_time) VALUES (?, ?, ?, ?)",
                    (user.id, chat.id, sent_message.message_id, unlock_time)
                )
                if update.message:
                    await conn.execute(
                        "INSERT OR REPLACE INTO cleanup_jobs (chat_id, message_id, delete_time) VALUES (?, ?, ?)",
                        (chat.id, update.message.message_id, start_delete_time)
                    )
                await conn.commit()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))
            else:
                logger.error(f"Failed to record jobs in DB for user {user.id}: {e}")
        except Exception as e:
            logger.error(f"Unexpected DB error recording jobs: {e}")
            break

    # Step 5: Schedule background tasks
    if update.message:
        start_job_data = {
            "chat_id": chat.id,
            "message_id": update.message.message_id
        }
        context.job_queue.run_once(
            delete_unprotected_task,
            start_delete_delay,
            data=start_job_data,
            name=f"delete_start_{chat.id}_{update.message.message_id}"
        )
        logger.info(f"Scheduled 30-second auto-deletion for user /start message {update.message.message_id} in chat {chat.id}.")

    job_data = {
        "user_id": user.id,
        "chat_id": chat.id,
        "message_id": sent_message.message_id
    }
    logger.info("Unlock timer started.")
    context.job_queue.run_once(unlock_task, current_delay, data=job_data, name=f"unlock_{user.id}_{chat.id}")


async def set_protected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the protected message text."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    text = update.message.text.partition(' ')[2]
    if not text:
        await update.message.reply_text("Please provide the text. Example:\n/setprotected Hello World")
        return
    async with get_db() as conn:
        await conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('protected', ?)", (text,))
        await conn.commit()
    CONFIG_CACHE["protected"] = text
    await update.message.reply_text("Protected message updated successfully!")


async def set_unprotected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the unprotected message text."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    text = update.message.text.partition(' ')[2]
    if not text:
        await update.message.reply_text("Please provide the text. Example:\n/setunprotected Hello World")
        return
    async with get_db() as conn:
        await conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('unprotected', ?)", (text,))
        await conn.commit()
    CONFIG_CACHE["unprotected"] = text
    await update.message.reply_text("Unprotected message updated successfully!")

async def set_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the unlock delay timer."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    text = update.message.text.partition(' ')[2]
    try:
        delay = float(text)
    except ValueError:
        await update.message.reply_text("Please provide a valid number of seconds. Example:\n/settimer 7.5 or /settimer 10")
        return
    
    async with get_db() as conn:
        await conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('delay', ?)", (str(delay),))
        await conn.commit()
    CONFIG_CACHE["delay"] = str(delay)
    await update.message.reply_text(f"Timer successfully updated to {delay} seconds!")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to broadcast a message to all registered bot users."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    
    broadcast_text = update.message.text.partition(' ')[2]
    if not broadcast_text:
        await update.message.reply_text("Please provide text to broadcast. Example:\n/broadcast Hello everyone!")
        return

    status_msg = await update.message.reply_text("Starting broadcast...")
    
    async with get_db() as conn:
        async with conn.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()

    total_users = len(rows)
    successful = 0
    failed = 0

    for row in rows:
        target_user_id = row[0]
        try:
            await context.bot.send_message(chat_id=target_user_id, text=broadcast_text)
            successful += 1
            await asyncio.sleep(0.04)  # Rate limiting safety delay
        except TelegramError as e:
            logger.warning(f"Broadcast failed for user {target_user_id}: {e}")
            failed += 1

    await status_msg.edit_text(
        f"✅ Broadcast Completed!\n\n"
        f"📊 Total Users: {total_users}\n"
        f"🟢 Delivered: {successful}\n"
        f"🔴 Failed/Blocked: {failed}"
    )

async def recover_jobs(application):
    """Recover pending jobs and cleanup tasks from the database after a restart."""
    now = datetime.now().timestamp()
    
    # 1. Recover unlock active_jobs
    async with get_db() as conn:
        async with conn.execute("SELECT user_id, chat_id, message_id, unlock_time FROM active_jobs") as cursor:
            rows = await cursor.fetchall()
        
    for row in rows:
        user_id, chat_id, message_id, unlock_time = row
        remaining = unlock_time - now
        
        job_data = {
            "user_id": user_id,
            "chat_id": chat_id,
            "message_id": message_id
        }
        
        if remaining <= 0:
            logger.info(f"Job for user {user_id} in chat {chat_id} is overdue. Running immediately.")
            application.job_queue.run_once(unlock_task, 1.0, data=job_data, name=f"unlock_{user_id}_{chat_id}")
        else:
            logger.info(f"Recovering job for user {user_id} in chat {chat_id}. Time remaining: {remaining:.2f}s")
            application.job_queue.run_once(unlock_task, remaining, data=job_data, name=f"unlock_{user_id}_{chat_id}")

    # 2. Recover auto-delete cleanup_jobs
    async with get_db() as conn:
        async with conn.execute("SELECT chat_id, message_id, delete_time FROM cleanup_jobs") as cursor:
            cleanup_rows = await cursor.fetchall()

    for row in cleanup_rows:
        chat_id, message_id, delete_time = row
        remaining = delete_time - now

        cleanup_job_data = {
            "chat_id": chat_id,
            "message_id": message_id
        }

        if remaining <= 0:
            logger.info(f"Cleanup job for message {message_id} in chat {chat_id} is overdue. Running immediately.")
            application.job_queue.run_once(delete_unprotected_task, 1.0, data=cleanup_job_data, name=f"delete_unprotected_{chat_id}_{message_id}")
        else:
            logger.info(f"Recovering cleanup job for message {message_id} in chat {chat_id}. Time remaining: {remaining:.2f}s")
            application.job_queue.run_once(delete_unprotected_task, remaining, data=cleanup_job_data, name=f"delete_unprotected_{chat_id}_{message_id}")


async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_webserver():
    """Starts a dummy web server so Render detects open port instantly."""
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Dummy web server started on port {port}")

async def post_init(application):
    """Run this after the bot initializes."""
    await start_webserver()  # Bind port IMMEDIATELY for Render port scanner
    await init_db()
    await load_config_cache()
    await recover_jobs(application)

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is missing. Please check your .env file.")
        return

    logger.info("Bot starting...")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setprotected", set_protected))
    application.add_handler(CommandHandler("setunprotected", set_unprotected))
    application.add_handler(CommandHandler("settimer", set_timer))
    application.add_handler(CommandHandler("broadcast", broadcast_command))

    application.run_polling()

if __name__ == "__main__":
    main()
