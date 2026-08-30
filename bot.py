import sqlite3
import logging
import os
from aiohttp import web
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import TelegramError

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
UNLOCK_DELAY = int(os.getenv("UNLOCK_DELAY", 10))
ADMIN_ID = int(os.getenv("ADMIN_ID", 8984398175))

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "bot_data.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
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
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.commit()

async def unlock_task(context: ContextTypes.DEFAULT_TYPE):
    """The task that runs after 10 seconds to delete the protected message and send the unprotected one."""
    job = context.job
    user_id = job.data["user_id"]
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]

    logger.info(f"Running unlock task for user {user_id} in chat {chat_id}")

    # Remove from DB first so if something crashes below, we don't end up in an infinite retry loop
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to remove job from DB: {e}")

    # 1. Delete protected message
    deleted = False
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Protected message {message_id} deleted successfully.")
        deleted = True
    except TelegramError as e:
        logger.warning(f"Failed to delete message {message_id}: {e}")
        # Even if deletion fails (e.g. user deleted it already), we might still want to send the unprotected message.
        # But if it's an error like bot was blocked, sending might also fail. We'll proceed to try sending anyway.
    
    # 2. Send unprotected message
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = 'unprotected'")
        row = cursor.fetchone()
        
    if row:
        unprotected_text = row[0]
    else:
        unprotected_text = "Thanks for the corporateion, now forward this message as you wish ."
        
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=unprotected_text,
            protect_content=False
        )
        logger.info(f"Unprotected message sent to {chat_id}.")
    except TelegramError as e:
        logger.error(f"Failed to send unprotected message to {chat_id}: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or not chat:
        return
    
    logger.info(f"User {user.id} started the bot in chat {chat.id}")

    # Check if a job is already running for this user in this chat
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT unlock_time FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user.id, chat.id))
        row = cursor.fetchone()
        
    if row:
        logger.info(f"User {user.id} already has a pending job. Ignoring duplicate start.")
        return

    # Send protected message
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = 'protected'")
        row = cursor.fetchone()

    if row:
        protected_text = row[0]
    else:
        protected_text = (
            "To forward this message tap this link-\n\n"
            "https://t.me/sae_plays/3 (stay for 5 sec)\n\n"
            "Then come back to this bot to forward this message as you wish ."
        )
    
    try:
        sent_message = await context.bot.send_message(
            chat_id=chat.id,
            text=protected_text,
            protect_content=True
        )
        logger.info("Protected message sent.")
    except TelegramError as e:
        logger.error(f"Failed to send protected message: {e}")
        return

    # Calculate time and save to DB
    now = datetime.now().timestamp()
    unlock_time = now + UNLOCK_DELAY

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO active_jobs (user_id, chat_id, message_id, unlock_time) VALUES (?, ?, ?, ?)",
            (user.id, chat.id, sent_message.message_id, unlock_time)
        )
        conn.commit()

    # Schedule the background task
    job_data = {
        "user_id": user.id,
        "chat_id": chat.id,
        "message_id": sent_message.message_id
    }
    
    logger.info("Unlock timer started.")
    context.job_queue.run_once(unlock_task, UNLOCK_DELAY, data=job_data, name=f"unlock_{user.id}_{chat.id}")


async def set_protected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the protected message text."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    text = update.message.text.partition(' ')[2]
    if not text:
        await update.message.reply_text("Please provide the text. Example:\n/setprotected Hello World")
        return
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('protected', ?)", (text,))
        conn.commit()
    await update.message.reply_text("Protected message updated successfully for today!")


async def set_unprotected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the unprotected message text."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    text = update.message.text.partition(' ')[2]
    if not text:
        await update.message.reply_text("Please provide the text. Example:\n/setunprotected Hello World")
        return
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('unprotected', ?)", (text,))
        conn.commit()
    await update.message.reply_text("Unprotected message updated successfully for today!")


def recover_jobs(application):
    """Recover pending jobs from the database after a restart."""
    now = datetime.now().timestamp()
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, chat_id, message_id, unlock_time FROM active_jobs")
        rows = cursor.fetchall()
        
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
            # Run immediately (using a small delay like 1s to allow bot to fully initialize)
            application.job_queue.run_once(unlock_task, 1.0, data=job_data, name=f"unlock_{user_id}_{chat_id}")
        else:
            logger.info(f"Recovering job for user {user_id} in chat {chat_id}. Time remaining: {remaining:.2f}s")
            application.job_queue.run_once(unlock_task, remaining, data=job_data, name=f"unlock_{user_id}_{chat_id}")


async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_webserver():
    """Starts a dummy web server so Render doesn't shut down the bot."""
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Dummy web server started on port {port}")

async def post_init(application):
    """Run this after the bot initializes."""
    await start_webserver()

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is missing. Please check your .env file.")
        return

    init_db()
    logger.info("Bot starting...")

    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setprotected", set_protected))
    application.add_handler(CommandHandler("setunprotected", set_unprotected))

    # Recover jobs right after app starts
    recover_jobs(application)

    application.run_polling()

if __name__ == "__main__":
    main()
