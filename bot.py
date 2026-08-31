import aiosqlite
import asyncio
import logging
import os
import sqlite3
from aiohttp import web
from contextlib import asynccontextmanager
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import TelegramError

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 8984398175))
FORWARD_LINK = os.getenv("FORWARD_LINK", "https://t.me/j4d3r_clips/4")

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "bot_data.db"
DB_TIMEOUT = 60.0

# In-Memory Cache for Zero-Disk-Read Config Lookup
CONFIG_CACHE = {
    "protected": None
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
            CREATE TABLE IF NOT EXISTS cleanup_jobs (
                chat_id INTEGER,
                start_message_id INTEGER,
                bot_message_id INTEGER,
                delete_time REAL,
                PRIMARY KEY (chat_id, bot_message_id)
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

async def auto_delete_pair_task(context: ContextTypes.DEFAULT_TYPE):
    """Deletes regular user's /start command message and the bot's response message after 120 seconds."""
    job = context.job
    chat_id = job.data["chat_id"]
    start_message_id = job.data.get("start_message_id")
    bot_message_id = job.data["bot_message_id"]

    logger.info(f"Running 120-second auto-delete task for chat {chat_id}")

    # Remove task entry from database
    for attempt in range(5):
        try:
            async with get_db() as conn:
                await conn.execute("DELETE FROM cleanup_jobs WHERE chat_id = ? AND bot_message_id = ?", (chat_id, bot_message_id))
                await conn.commit()
            break
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))
            else:
                logger.error(f"Failed to remove cleanup job from DB: {e}")

    # 1. Delete user's /start command message if present
    if start_message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=start_message_id)
            logger.info(f"Deleted user's /start message {start_message_id} in chat {chat_id}.")
        except TelegramError as e:
            logger.warning(f"Could not delete start message {start_message_id} in chat {chat_id}: {e}")

    # 2. Delete bot's message
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=bot_message_id)
        logger.info(f"Deleted bot message {bot_message_id} in chat {chat_id}.")
    except TelegramError as e:
        logger.warning(f"Could not delete bot message {bot_message_id} in chat {chat_id}: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command. Sends protected message with direct button URL, and auto-deletes after 120s (non-admin)."""
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or not chat:
        return
    
    now = datetime.now().timestamp()
    logger.info(f"User {user.id} started the bot in chat {chat.id}")

    # Register user (including admin) in DB
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

    # Prepare message content & direct button URL
    default_text = "To forward this message please click the following button:\n\n⬇️             ⬇️             ⬇️"
    protected_text = CONFIG_CACHE.get("protected") or default_text

    keyboard = [
        [InlineKeyboardButton("🔗 CLICK HERE TO FORWARD 🔗", url=FORWARD_LINK)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    start_msg_id = update.message.message_id if update.message else None

    # Check if Admin (Total Exemption)
    if user.id == ADMIN_ID:
        logger.info(f"Admin {user.id} executed /start. Delivering unprotected persistent content directly.")
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=protected_text,
                reply_markup=reply_markup,
                protect_content=False
            )
        except TelegramError as e:
            logger.error(f"Failed to send start message to admin: {e}")
        return

    # Regular User Flow: Send protected message & schedule 120s auto-deletion
    try:
        sent_message = await context.bot.send_message(
            chat_id=chat.id,
            text=protected_text,
            reply_markup=reply_markup,
            protect_content=True
        )
        logger.info(f"Protected message sent to user {user.id} in chat {chat.id}.")
        asyncio.create_task(increment_stat("protected_sent"))
    except TelegramError as e:
        logger.error(f"Failed to send protected message to {chat.id}: {e}")
        return

    # Record 120-second auto-deletion in DB for restart safety
    delete_delay = 120.0
    delete_time = now + delete_delay

    for attempt in range(5):
        try:
            async with get_db() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO cleanup_jobs (chat_id, start_message_id, bot_message_id, delete_time) VALUES (?, ?, ?, ?)",
                    (chat.id, start_msg_id, sent_message.message_id, delete_time)
                )
                await conn.commit()
            break
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(0.1 * (attempt + 1))
            else:
                logger.error(f"Failed to record cleanup job in DB: {e}")

    # Schedule 120-second auto-deletion task
    cleanup_job_data = {
        "chat_id": chat.id,
        "start_message_id": start_msg_id,
        "bot_message_id": sent_message.message_id
    }
    context.job_queue.run_once(
        auto_delete_pair_task,
        delete_delay,
        data=cleanup_job_data,
        name=f"delete_pair_{chat.id}_{sent_message.message_id}"
    )
    logger.info(f"Scheduled 120-second auto-deletion for chat {chat.id}.")


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
        f"🟢 Delivered: {successful}\n"
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

    protected_sent = stats_dict.get("protected_sent", 0)

    msg = (
        "📊 *Bot Statistics*\n\n"
        f"👥 *Users Started Bot:* {total_users}\n"
        f"🔒 *Protected Messages Delivered:* {protected_sent}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def recover_jobs(application):
    """Recover pending auto-delete cleanup tasks from the database after a restart."""
    now = datetime.now().timestamp()
    
    async with get_db() as conn:
        async with conn.execute("SELECT chat_id, start_message_id, bot_message_id, delete_time FROM cleanup_jobs") as cursor:
            cleanup_rows = await cursor.fetchall()

    for row in cleanup_rows:
        chat_id, start_message_id, bot_message_id, delete_time = row
        remaining = delete_time - now

        cleanup_job_data = {
            "chat_id": chat_id,
            "start_message_id": start_message_id,
            "bot_message_id": bot_message_id
        }

        if remaining <= 0:
            logger.info(f"Cleanup job for message {bot_message_id} in chat {chat_id} is overdue. Running immediately.")
            application.job_queue.run_once(auto_delete_pair_task, 1.0, data=cleanup_job_data, name=f"delete_pair_{chat_id}_{bot_message_id}")
        else:
            logger.info(f"Recovering cleanup job for message {bot_message_id} in chat {chat_id}. Time remaining: {remaining:.2f}s")
            application.job_queue.run_once(auto_delete_pair_task, remaining, data=cleanup_job_data, name=f"delete_pair_{chat_id}_{bot_message_id}")


async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_webserver():
    """Starts a lightweight web server for Render port binding health checks."""
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web server started on port {port}")

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
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("stats", stats_command))

    application.run_polling()

if __name__ == "__main__":
    main()
