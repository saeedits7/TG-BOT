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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
UNLOCK_DELAY = float(os.getenv("UNLOCK_DELAY", 7.5))
ADMIN_ID = int(os.getenv("ADMIN_ID", 8984398175))
FORWARD_LINK = os.getenv("FORWARD_LINK", "https://t.me/sae_plays/3")

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
                start_message_id INTEGER,
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
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
            """
        )
        await conn.commit()

async def increment_stat(key: str, count: int = 1):
    for attempt in range(5):
        try:
            async with get_db() as conn:
                await conn.execute(
                    "INSERT INTO stats (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = value + ?",
                    (key, count, count)
                )
                await conn.commit()
            break
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))
            else:
                logger.error(f"Failed to increment stat {key}: {e}")


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

async def process_unlock(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, message_id: int, start_message_id: int = None):
    """Deletes protected message & /start message, and sends unprotected message."""
    logger.info(f"Processing unlock for user {user_id} in chat {chat_id}")

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

    # 2. Delete /start message if provided
    if start_message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=start_message_id)
            logger.info(f"Start message {start_message_id} deleted successfully.")
        except TelegramError as e:
            logger.warning(f"Failed to delete start message {start_message_id}: {e}")

    # 3. Send unprotected message (from RAM cache)
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

async def unlock_task(context: ContextTypes.DEFAULT_TYPE):
    """The task that runs after delay to unlock if user didn't tap button."""
    job = context.job
    user_id = job.data["user_id"]
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]
    start_message_id = job.data.get("start_message_id")

    await process_unlock(context, user_id, chat_id, message_id, start_message_id)

async def auto_expire_active_job(context: ContextTypes.DEFAULT_TYPE):
    """Resets active job restriction after 2 minutes if user hasn't clicked button."""
    job = context.job
    user_id = job.data["user_id"]
    chat_id = job.data["chat_id"]
    for attempt in range(5):
        try:
            async with get_db() as conn:
                await conn.execute("DELETE FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
                await conn.commit()
            break
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))
    logger.info(f"Active job restriction for user {user_id} automatically expired and reset after 2 minutes.")

async def forward_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles tap on 'CLICK HERE TO FORWARD' button."""
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id

    logger.info(f"User {user_id} clicked forward button in chat {chat_id}")
    asyncio.create_task(increment_stat("button_taps"))

    # Answer callback query so button loading animation stops cleanly
    try:
        await query.answer(text="Unlocking message...")
    except Exception as e:
        logger.warning(f"Could not answer callback query: {e}")

    # Retrieve and remove active job info for this user
    start_message_id = None
    message_id = query.message.message_id if query.message else None

    for attempt in range(5):
        try:
            async with get_db() as conn:
                async with conn.execute("SELECT message_id, start_message_id FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        message_id = row[0]
                        start_message_id = row[1]
                await conn.commit()
            break
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))

    # Cancel scheduled job timers if running
    jobs = context.job_queue.get_jobs_by_name(f"unlock_{user_id}_{chat_id}")
    for job in jobs:
        job.schedule_removal()
    expire_jobs = context.job_queue.get_jobs_by_name(f"expire_job_{user_id}_{chat_id}")
    for job in expire_jobs:
        job.schedule_removal()

    # Trigger immediate unlock & cleanup
    if message_id:
        await process_unlock(context, user_id, chat_id, message_id, start_message_id)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command with WAL mode, busy timeout, retries, and high concurrency resilience."""
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or not chat:
        return
    
    now = datetime.now().timestamp()
    logger.info(f"User {user.id} started the bot in chat {chat.id}")

    # Step 1: Register user (including admin) in DB
    for attempt in range(3):
        try:
            async with get_db() as conn:
                await conn.execute("INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?, ?)", (user.id, now))
                await conn.commit()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                await asyncio.sleep(0.1 * (attempt + 1))
        except Exception as e:
            logger.error(f"Unexpected DB error registering user: {e}")
            break

    # Admin bypass: Everything is unprotected for the admin (protect_content=False)
    if user.id == ADMIN_ID:
        logger.info(f"Admin {user.id} executed /start. Delivering unprotected content directly.")
        unprotected_text = CONFIG_CACHE.get("unprotected") or "Thanks for the cooperation, now forward this message as you wish."
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=unprotected_text,
                protect_content=False
            )
        except TelegramError as e:
            logger.error(f"Failed to send start message to admin: {e}")
        return

    # Check active job for regular user (resets after 2 minutes / 120 seconds)
    has_active_job = False
    for attempt in range(3):
        try:
            async with get_db() as conn:
                async with conn.execute("SELECT unlock_time FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user.id, chat.id)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        job_created_time = row[0]
                        if job_created_time > 0 and (now - job_created_time) < 120.0:
                            has_active_job = True
                        else:
                            # Job is older than 2 minutes (120s), expire and allow new start
                            await conn.execute("DELETE FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user.id, chat.id))
                await conn.commit()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                await asyncio.sleep(0.1 * (attempt + 1))
        except Exception as e:
            logger.error(f"Unexpected DB error checking active job: {e}")
            break

    if has_active_job:
        logger.info(f"User {user.id} already has a pending job (< 2 min old). Ignoring duplicate start.")
        return

    # Step 2: Fetch protected text & timer delay from memory cache (Zero Disk Read)
    default_text = "To forward this message please click the following button:\n\n⬇️             ⬇️             ⬇️"
    protected_text = CONFIG_CACHE.get("protected") or default_text

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

    start_msg_id = update.message.message_id if update.message else None

    # Construct dynamic redirect link for instant HTTP 302 redirection + clean-up trigger
    base_url = os.getenv("RENDER_EXTERNAL_URL", "https://tg-bot-2gkj.onrender.com").rstrip('/')
    redirect_url = f"{base_url}/redirect?user_id={user.id}&chat_id={chat.id}"

    # Create Glassmorphism / Link style URL button
    keyboard = [
        [InlineKeyboardButton("🔗 CLICK HERE TO FORWARD 🔗", url=redirect_url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Step 3: Send protected message (GUARANTEED delivery to user!)
    try:
        sent_message = await context.bot.send_message(
            chat_id=chat.id,
            text=protected_text,
            reply_markup=reply_markup,
            protect_content=True
        )
        logger.info("Protected message with inline button sent.")
        asyncio.create_task(increment_stat("protected_sent"))
    except TelegramError as e:
        logger.error(f"Failed to send protected message to {chat.id}: {e}")
        return

    # Step 4: Record active job in DB with timestamp & schedule 2-minute auto-expiration
    for attempt in range(5):
        try:
            async with get_db() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO active_jobs (user_id, chat_id, message_id, unlock_time, start_message_id) VALUES (?, ?, ?, ?, ?)",
                    (user.id, chat.id, sent_message.message_id, now, start_msg_id)
                )
                await conn.commit()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))
            else:
                logger.error(f"Failed to record active job in DB for user {user.id}: {e}")
        except Exception as e:
            logger.error(f"Unexpected DB error recording job: {e}")
            break

    # Schedule auto-expiration after 120 seconds (2 minutes)
    job_data = {"user_id": user.id, "chat_id": chat.id}
    context.job_queue.run_once(
        auto_expire_active_job,
        120.0,
        data=job_data,
        name=f"expire_job_{user.id}_{chat.id}"
    )

    logger.info(f"Active job registered for user {user.id}. Will auto-reset after 2 minutes if button not tapped.")


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
    """Admin command to broadcast a protected message to all registered bot users."""
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
        is_admin_target = (target_user_id == ADMIN_ID)
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=broadcast_text,
                protect_content=not is_admin_target
            )
            successful += 1
            await asyncio.sleep(0.04)  # Rate limiting safety delay
        except TelegramError as e:
            logger.warning(f"Broadcast failed for user {target_user_id}: {e}")
            failed += 1

    if successful > 0:
        asyncio.create_task(increment_stat("protected_sent", successful))

    await status_msg.edit_text(
        f"✅ Broadcast Completed!\n\n"
        f"📊 Total Users: {total_users}\n"
        f"🟢 Delivered (Protected): {successful}\n"
        f"🔴 Failed/Blocked: {failed}"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to view bot usage statistics."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    
    async with get_db() as conn:
        async with conn.execute("SELECT COUNT(*) FROM users") as cursor:
            user_count_row = await cursor.fetchone()
            total_users = user_count_row[0] if user_count_row else 0
            
        async with conn.execute("SELECT key, value FROM stats") as cursor:
            rows = await cursor.fetchall()
            stats_dict = dict(rows)

    button_taps = stats_dict.get("button_taps", 0)
    protected_sent = stats_dict.get("protected_sent", 0)

    msg = (
        "📊 *Bot Statistics*\n\n"
        f"👥 *Users Started Bot:* {total_users}\n"
        f"🔘 *Button Taps:* {button_taps}\n"
        f"🔒 *Protected Messages Delivered:* {protected_sent}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def recover_jobs(application):
    """Recover pending jobs and cleanup tasks from the database after a restart."""
    now = datetime.now().timestamp()
    
    # 1. Recover unlock active_jobs
    async with get_db() as conn:
        async with conn.execute("SELECT user_id, chat_id, message_id, unlock_time, start_message_id FROM active_jobs") as cursor:
            rows = await cursor.fetchall()
        
    for row in rows:
        user_id, chat_id, message_id, unlock_time, start_message_id = row
        remaining = unlock_time - now
        
        job_data = {
            "user_id": user_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "start_message_id": start_message_id
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


GLOBAL_APP = None

async def handle_redirect(request):
    """Handles button click web redirect, unlocks message instantly, and sends user to destination link."""
    user_id_str = request.query.get("user_id")
    chat_id_str = request.query.get("chat_id")
    
    if user_id_str and chat_id_str and GLOBAL_APP:
        try:
            user_id = int(user_id_str)
            chat_id = int(chat_id_str)
            asyncio.create_task(increment_stat("button_taps"))
            
            # Retrieve active job info from DB
            message_id = None
            start_message_id = None
            async with get_db() as conn:
                async with conn.execute("SELECT message_id, start_message_id FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        message_id = row[0]
                        start_message_id = row[1]
            
            # Trigger asynchronous message deletion and unlock
            if message_id:
                # Cancel timers if running
                jobs = GLOBAL_APP.job_queue.get_jobs_by_name(f"unlock_{user_id}_{chat_id}")
                for job in jobs:
                    job.schedule_removal()
                expire_jobs = GLOBAL_APP.job_queue.get_jobs_by_name(f"expire_job_{user_id}_{chat_id}")
                for job in expire_jobs:
                    job.schedule_removal()
                
                asyncio.create_task(process_unlock(GLOBAL_APP, user_id, chat_id, message_id, start_message_id))
        except Exception as e:
            logger.error(f"Error handling web redirect unlock: {e}")

    # Instant HTTP 302 Redirect to destination Telegram link
    return web.HTTPFound(location=FORWARD_LINK)

async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_webserver():
    """Starts the web server with redirect endpoint."""
    app = web.Application()
    app.router.add_get('/redirect', handle_redirect)
    app.router.add_route('*', '/{tail:.*}', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web server started on port {port}")

async def post_init(application):
    """Run this after the bot initializes."""
    global GLOBAL_APP
    GLOBAL_APP = application
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
    application.add_handler(CallbackQueryHandler(forward_button_callback, pattern="^forward_click$"))
    application.add_handler(CommandHandler("setprotected", set_protected))
    application.add_handler(CommandHandler("setunprotected", set_unprotected))
    application.add_handler(CommandHandler("settimer", set_timer))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("stats", stats_command))

    application.run_polling()

if __name__ == "__main__":
    main()
