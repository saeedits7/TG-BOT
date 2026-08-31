import aiosqlite
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
UNLOCK_DELAY = float(os.getenv("UNLOCK_DELAY", 7.5))
ADMIN_ID = int(os.getenv("ADMIN_ID", 8984398175))

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "bot_data.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as conn:
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
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await conn.commit()

async def unlock_task(context: ContextTypes.DEFAULT_TYPE):
    """The task that runs after delay to delete the protected message and send the unprotected one."""
    job = context.job
    user_id = job.data["user_id"]
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]

    logger.info(f"Running unlock task for user {user_id} in chat {chat_id}")

    # Remove from DB first so if something crashes below, we don't end up in an infinite retry loop
    try:
        async with aiosqlite.connect(DB_FILE) as conn:
            await conn.execute("DELETE FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
            await conn.commit()
    except Exception as e:
        logger.error(f"Failed to remove job from DB: {e}")

    # 1. Delete protected message
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Protected message {message_id} deleted successfully.")
    except TelegramError as e:
        logger.warning(f"Failed to delete message {message_id}: {e}")
    
    # 2. Send unprotected message
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT value FROM config WHERE key = 'unprotected'") as cursor:
            row = await cursor.fetchone()
        
    if row:
        unprotected_text = row[0]
    else:
        unprotected_text = "Thanks for the cooperation, now forward this message as you wish."
        
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
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT unlock_time FROM active_jobs WHERE user_id = ? AND chat_id = ?", (user.id, chat.id)) as cursor:
            row = await cursor.fetchone()
        
    if row:
        logger.info(f"User {user.id} already has a pending job. Ignoring duplicate start.")
        return

    # Send protected message
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT value FROM config WHERE key = 'protected'") as cursor:
            row = await cursor.fetchone()

    if row:
        protected_text = row[0]
    else:
        protected_text = (
            "To forward this message tap this link-\n\n"
            "https://t.me/sae_plays/3 (stay for 5-10 sec)"
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
    
    # Fetch dynamic delay or use default
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT value FROM config WHERE key = 'delay'") as cursor:
            row = await cursor.fetchone()
        
    if row:
        try:
            current_delay = float(row[0])
        except ValueError:
            current_delay = UNLOCK_DELAY
    else:
        current_delay = UNLOCK_DELAY

    unlock_time = now + current_delay

    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute(
            "INSERT INTO active_jobs (user_id, chat_id, message_id, unlock_time) VALUES (?, ?, ?, ?)",
            (user.id, chat.id, sent_message.message_id, unlock_time)
        )
        await conn.commit()

    # Schedule the background task
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
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('protected', ?)", (text,))
        await conn.commit()
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
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('unprotected', ?)", (text,))
        await conn.commit()
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
    
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('delay', ?)", (str(delay),))
        await conn.commit()
    await update.message.reply_text(f"Timer successfully updated to {delay} seconds!")


async def recover_jobs(application):
    """Recover pending jobs from the database after a restart."""
    now = datetime.now().timestamp()
    
    async with aiosqlite.connect(DB_FILE) as conn:
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
    await init_db()
    await recover_jobs(application)
    await start_webserver()

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is missing. Please check your .env file.")
        return

    logger.info("Bot starting...")

    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setprotected", set_protected))
    application.add_handler(CommandHandler("setunprotected", set_unprotected))
    application.add_handler(CommandHandler("settimer", set_timer))

    application.run_polling()

if __name__ == "__main__":
    main()
