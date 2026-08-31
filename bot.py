import logging
import os
from aiohttp import web
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, AIORateLimiter
from telegram.error import TelegramError

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_UNLOCK_DELAY = float(os.getenv("UNLOCK_DELAY", 7.5))
ADMIN_ID = int(os.getenv("ADMIN_ID", 8984398175))

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-Memory State (Replaces SQLite for maximum speed and zero I/O)
config = {
    "delay": DEFAULT_UNLOCK_DELAY,
    "protected": "To forward this message tap this link-\n\nhttps://t.me/sae_plays/3 (stay for 5-10 sec)",
    "unprotected": "Thanks for the cooperation, now forward this message as you wish."
}

# Sets to prevent duplicate jobs
active_users = set()

async def cleanup_task(context: ContextTypes.DEFAULT_TYPE):
    """The task that runs after 2 minutes to delete the unprotected message and start command."""
    job = context.job
    user_id = job.data["user_id"]
    chat_id = job.data["chat_id"]
    command_message_id = job.data.get("command_message_id")
    unprotected_message_id = job.data.get("unprotected_message_id")

    logger.info(f"Running cleanup task for user {user_id} in chat {chat_id}")

    if unprotected_message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=unprotected_message_id)
            logger.info(f"Unprotected message {unprotected_message_id} deleted successfully.")
        except TelegramError as e:
            logger.warning(f"Failed to delete unprotected message {unprotected_message_id}: {e}")

    if command_message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=command_message_id)
            logger.info(f"Command message {command_message_id} deleted successfully.")
        except TelegramError as e:
            logger.warning(f"Failed to delete command message {command_message_id}: {e}")

async def unlock_task(context: ContextTypes.DEFAULT_TYPE):
    """The task that runs after the delay to delete the protected message and send the unprotected one."""
    job = context.job
    user_id = job.data["user_id"]
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]
    command_message_id = job.data.get("command_message_id")

    logger.info(f"Running unlock task for user {user_id} in chat {chat_id}")

    # Remove from active users so they can start a new request if needed
    active_users.discard((user_id, chat_id))

    # 1. Delete protected message
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Protected message {message_id} deleted successfully.")
    except TelegramError as e:
        logger.warning(f"Failed to delete message {message_id}: {e}")
    
    # 2. Send unprotected message
    unprotected_text = config["unprotected"]
        
    try:
        sent_unprotected = await context.bot.send_message(
            chat_id=chat_id,
            text=unprotected_text,
            protect_content=False
        )
        logger.info(f"Unprotected message sent to {chat_id}.")
        
        # Schedule cleanup task
        cleanup_delay = 120 # 2 minutes
        cleanup_data = {
            "user_id": user_id,
            "chat_id": chat_id,
            "command_message_id": command_message_id,
            "unprotected_message_id": sent_unprotected.message_id
        }
        context.job_queue.run_once(cleanup_task, cleanup_delay, data=cleanup_data, name=f"cleanup_{user_id}_{chat_id}")
        
    except TelegramError as e:
        logger.error(f"Failed to send unprotected message to {chat_id}: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or not chat or not update.message:
        return
        
    command_message_id = update.message.message_id
    
    logger.info(f"User {user.id} started the bot in chat {chat.id}")

    # Check if a job is already running for this user in this chat
    if (user.id, chat.id) in active_users:
        logger.info(f"User {user.id} already has a pending job. Ignoring duplicate start.")
        return

    # Send protected message
    protected_text = config["protected"]
    
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

    # Add to active users
    active_users.add((user.id, chat.id))
    
    current_delay = config["delay"]

    # Schedule the background task
    job_data = {
        "user_id": user.id,
        "chat_id": chat.id,
        "message_id": sent_message.message_id,
        "command_message_id": command_message_id
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
    
    config["protected"] = text
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
        
    config["unprotected"] = text
    await update.message.reply_text("Unprotected message updated successfully!")

async def set_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the unlock delay timer."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    text = update.message.text.partition(' ')[2]
    if not text or not text.replace('.', '', 1).isdigit():
        await update.message.reply_text("Please provide a valid number of seconds. Example:\n/settimer 7.5")
        return
    
    config["delay"] = float(text)
    await update.message.reply_text(f"Timer successfully updated to {config['delay']} seconds!")


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

    logger.info("Bot starting...")

    application = ApplicationBuilder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setprotected", set_protected))
    application.add_handler(CommandHandler("setunprotected", set_unprotected))
    application.add_handler(CommandHandler("settimer", set_timer))

    # We pass drop_pending_updates=True so that old instances/conflicts don't replay old messages
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
