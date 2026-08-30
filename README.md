# Telegram Protected Content Bot

This bot implements a custom `/start` workflow using the `python-telegram-bot` framework.

## Features

- Greets the user with a protected message (`protect_content=True`) upon `/start`.
- Waits exactly 10 seconds (configurable) in the background without blocking other users.
- After 10 seconds, deletes the protected message and sends an unprotected, forwardable replacement.
- Uses SQLite to persist jobs. If the bot crashes or restarts during a user's 10-second wait, it will resume the timer upon restart and ensure the message unlocks.
- Fully asynchronous so multiple users can interact with the bot concurrently.

## Setup & Execution

### 1. Requirements

- Python 3.8+
- Dependencies installed in `requirements.txt`

### 2. Environment Variables

Create a `.env` file in the root directory (already created) with the following content:

```ini
BOT_TOKEN=your_bot_token_here
UNLOCK_DELAY=10
```

### 3. Running the Bot

To start the bot, activate your virtual environment (if used) and run `bot.py`:

```powershell
.\venv\Scripts\activate
python bot.py
```

### 4. How to Test

1. Open your bot in Telegram and click `START` or type `/start`.
2. Observe the protected message arriving (you shouldn't be able to forward it).
3. Wait exactly 10 seconds.
4. The protected message will vanish and be replaced with the unprotected message.
5. If you want to test restart recovery: hit `/start`, wait 3 seconds, stop the bot script (Ctrl+C), start the script again, and watch it automatically resume the timer and unlock the message.
