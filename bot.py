from pathlib import Path
import zipfile, textwrap, os

base = Path("/mnt/data/telegram_auto_delete_bot")
base.mkdir(parents=True, exist_ok=True)

files = {
"bot.py": r'''
import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path

from telegram import Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.sqlite3")
MAX_MINUTES = 24 * 60
POLL_SECONDS = 1

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("auto-delete-bot")


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                delay_seconds INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_deletions (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                due_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        self.conn.commit()

    def get_delay(self, chat_id: int) -> int:
        row = self.conn.execute(
            "SELECT delay_seconds FROM settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return int(row["delay_seconds"]) if row else 0

    def set_delay(self, chat_id: int, seconds: int) -> None:
        self.conn.execute(
            """INSERT INTO settings(chat_id, delay_seconds) VALUES(?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET delay_seconds=excluded.delay_seconds""",
            (chat_id, seconds),
        )
        self.conn.commit()

    def schedule(self, chat_id: int, message_id: int, delay: int) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO scheduled_deletions
               (chat_id, message_id, due_at, attempts)
               VALUES (?, ?, ?, 0)""",
            (chat_id, message_id, time.time() + delay),
        )
        self.conn.commit()

    def due_messages(self, now: float):
        return self.conn.execute(
            """SELECT chat_id, message_id, attempts
               FROM scheduled_deletions WHERE due_at <= ?
               ORDER BY due_at LIMIT 100""",
            (now,),
        ).fetchall()

    def remove_scheduled(self, chat_id: int, message_id: int) -> None:
        self.conn.execute(
            "DELETE FROM scheduled_deletions WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        self.conn.commit()

    def retry_later(self, chat_id: int, message_id: int, attempts: int) -> None:
        if attempts >= 20:
            self.remove_scheduled(chat_id, message_id)
            return
        self.conn.execute(
            """UPDATE scheduled_deletions
               SET attempts=?, due_at=? WHERE chat_id=? AND message_id=?""",
            (attempts, time.time() + min(30, 2 ** min(attempts, 5)),
             chat_id, message_id),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


db = Database(DATABASE_PATH)


async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type not in ("group", "supergroup"):
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


async def temporary_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
    seconds: int = 12
) -> None:
    message = update.effective_message
    if not message:
        return
    try:
        sent = await message.reply_text(text)
        db.schedule(message.chat_id, sent.message_id, seconds)
    except TelegramError as exc:
        logger.warning("Could not send bot reply: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await temporary_reply(
        update, context,
        "Hi! I am an automatic message-deletion bot.\\n"
        "Add me to a group as an admin with Delete Messages permission, "
        "then use /settime 1 to delete messages after one minute.\\n"
        "Use /help to see commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await temporary_reply(
        update, context,
        "Auto Delete Bot commands:\\n"
        "/settime 1 — delete after 1 minute\\n"
        "/settime 2 — delete after 2 minutes\\n"
        "/settime 10 — delete after 10 minutes\\n"
        "/off — turn automatic deletion off\\n"
        "/time — show this group's current setting\\n"
        "/help — show this help\\n\\n"
        "Only group admins can change the timer. "
        "Allowed range: 1 to 1440 minutes."
    )


async def settime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await temporary_reply(update, context, "Use this command inside a group.")
        return
    if not await is_group_admin(update, context):
        await temporary_reply(update, context, "Only group admins can change this setting.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await temporary_reply(update, context, "Usage: /settime 1  (time is in minutes)")
        return

    minutes = int(context.args[0])
    if not 1 <= minutes <= MAX_MINUTES:
        await temporary_reply(
            update, context, f"Choose a time from 1 to {MAX_MINUTES} minutes."
        )
        return

    db.set_delay(chat.id, minutes * 60)
    await temporary_reply(
        update, context,
        f"Auto-delete enabled. New messages will be deleted after {minutes} minute(s)."
    )


async def off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await temporary_reply(update, context, "Use this command inside a group.")
        return
    if not await is_group_admin(update, context):
        await temporary_reply(update, context, "Only group admins can change this setting.")
        return
    db.set_delay(chat.id, 0)
    await temporary_reply(update, context, "Automatic deletion is now OFF.")


async def show_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await temporary_reply(update, context, "Use this command inside a group.")
        return
    seconds = db.get_delay(chat.id)
    if seconds == 0:
        text = "Auto-delete is OFF in this group. An admin can enable it with /settime 1."
    else:
        text = f"Current auto-delete timer: {seconds // 60} minute(s)."
    await temporary_reply(update, context, text)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in ("group", "supergroup"):
        return
    delay = db.get_delay(chat.id)
    if delay <= 0:
        return
    # Store the message ID and due time in SQLite so pending deletions survive restarts.
    db.schedule(chat.id, message.message_id, delay)


async def deletion_worker(application: Application) -> None:
    logger.info("Deletion worker started")
    while True:
        rows = db.due_messages(time.time())
        for row in rows:
            chat_id = int(row["chat_id"])
            message_id = int(row["message_id"])
            attempts = int(row["attempts"])
            try:
                await application.bot.delete_message(
                    chat_id=chat_id, message_id=message_id
                )
                db.remove_scheduled(chat_id, message_id)
            except (BadRequest, Forbidden) as exc:
                # Permanent failures include messages that no longer exist or cannot be deleted.
                logger.info("Cannot delete %s/%s: %s", chat_id, message_id, exc)
                db.remove_scheduled(chat_id, message_id)
            except TelegramError as exc:
                logger.warning("Temporary Telegram error for %s/%s: %s",
                               chat_id, message_id, exc)
                db.retry_later(chat_id, message_id, attempts + 1)
            except Exception:
                logger.exception("Unexpected deletion error")
                db.retry_later(chat_id, message_id, attempts + 1)
        await asyncio.sleep(POLL_SECONDS)


async def post_init(application: Application) -> None:
    application.bot_data["deletion_worker"] = asyncio.create_task(
        deletion_worker(application)
    )


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("deletion_worker")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    db.close()


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in your cloud environment variables.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start), group=0)
    application.add_handler(CommandHandler("help", help_command), group=0)
    application.add_handler(CommandHandler("settime", settime), group=0)
    application.add_handler(CommandHandler("off", off), group=0)
    application.add_handler(CommandHandler("time", show_time), group=0)

    # Group 1 also receives command messages, so commands are included in auto-delete.
    application.add_handler(
        MessageHandler(filters.ALL, on_group_message),
        group=1,
    )

    logger.info("Bot is starting with polling")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
'''.strip() + "\n",
"requirements.txt": "python-telegram-bot==22.8\n",
"Dockerfile": """FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
CMD ["python", "bot.py"]
""",
".env.example": """# Get this token from Telegram's official BotFather bot.
BOT_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE

# On a cloud host, point this to a mounted persistent disk.
DATABASE_PATH=/var/data/bot.sqlite3
""",
"render.yaml": """services:
  - type: worker
    name: telegram-auto-delete-bot
    runtime: docker
    plan: starter
    dockerfilePath: ./Dockerfile
    dockerContext: .
    autoDeploy: true
    disk:
      name: telegram-bot-data
      mountPath: /var/data
      sizeGB: 1
    envVars:
      - key: BOT_TOKEN
        sync: false
      - key: DATABASE_PATH
        value: /var/data/bot.sqlite3
""",
".gitignore": """.env
__pycache__/
*.py[cod]
*.sqlite3
.venv/
venv/
""",
"README.md": r"""
# Telegram Auto Delete Bot

A Python Telegram group bot that deletes ordinary group messages after a configurable delay. Each group has its own timer, stored in SQLite.

## Commands

- `/settime 1` — delete new messages after 1 minute
- `/settime 2` — delete new messages after 2 minutes
- `/settime 15` — delete new messages after 15 minutes
- `/off` — disable auto-delete
- `/time` — show the current setting
- `/help` — show help

Only group administrators can change the timer. The timer range is 1–1440 minutes. Auto-delete is OFF until an admin enables it.

## 1. Create your Telegram bot

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow its instructions.
3. Copy the bot token. Keep it secret; never post it in a group or commit it to GitHub.
4. In BotFather, open `/mybots` → your bot → Bot Settings → Group Privacy. You can leave privacy mode enabled if you add the bot as a group administrator; Telegram delivers all group messages to administrator bots.
5. Add the bot to your group and promote it to administrator with **Delete Messages** permission.

Telegram's rules require the bot to have the appropriate administrator rights to delete other members' group messages.

## 2. Deploy in the cloud (Render background worker)

This project includes `render.yaml` and a Dockerfile.

1. Put the project files in a GitHub repository.
2. In Render, create a new Blueprint and connect that repository.
3. Review the background worker and persistent disk in the Blueprint.
4. Set the secret environment variable `BOT_TOKEN` to your BotFather token.
5. Deploy. Keep the worker running; a sleeping web service is not suitable for a bot that must process messages continuously.
6. Check the worker logs. You should see `Bot is starting with polling`.

The included Render configuration uses a paid always-on worker plan and a persistent disk. Cloud provider plans and pricing can change; check the provider's current pricing before deploying. If using another provider, run the Dockerfile as a continuously running worker and mount persistent storage at `/var/data`.

## 3. Use it in your group

In the group, an administrator sends:

```text
/settime 1
```

New messages will be scheduled for deletion after one minute. For two minutes, use `/settime 2`. Use `/off` to stop scheduling new deletions.

## Local test (optional)

Python 3.11+:

```bash
python -m venv .venv
```

Activate it, then:

```bash
pip install -r requirements.txt
```

Set environment variables:

```bash
# macOS/Linux
export BOT_TOKEN="YOUR_TOKEN"
export DATABASE_PATH="./bot.sqlite3"
python bot.py
```

PowerShell:

```powershell
$env:BOT_TOKEN="YOUR_TOKEN"
$env:DATABASE_PATH=".\bot.sqlite3"
python bot.py
```

## Important limitations

- Telegram generally only allows a bot to delete messages that are less than 48 hours old.
- The bot needs administrator permission to delete messages sent by other members.
- This bot schedules new messages only; it does not retroactively delete old messages.
- Auto-delete is OFF by default. A group admin must enable it with `/settime`.
- Deletion can fail for messages Telegram does not permit the bot to delete, or if permissions are removed.
- The SQLite database stores group timer settings and pending message IDs/times. It does not store message contents.
- Because the bot can read group messages in order to schedule their deletion, only add it to groups where members understand and accept that behavior.
""".strip() + "\n"
}

for name, content in files.items():
    (base / name).write_text(content, encoding="utf-8")

zip_path = Path("/mnt/data/telegram_auto_delete_bot.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for path in base.rglob("*"):
        if path.is_file():
            zf.write(path, path.relative_to(base.parent))

print(f"Created project: {zip_path}")
print("Files:", ", ".join(files.keys()))
