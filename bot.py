import asyncio
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from telegram.request import HTTPXRequest
from telegram import Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = "telegram"
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "/tmp/bot.sqlite3")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("auto-delete-bot")


class Store:
    """Use Upstash Redis when configured; otherwise use temporary local SQLite."""
    def __init__(self):
        self.redis = bool(REDIS_URL and REDIS_TOKEN)
        if self.redis:
            logger.info("Using Upstash Redis for persistent settings and schedules")
        else:
            import sqlite3
            Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY, delay_seconds INTEGER NOT NULL)""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS scheduled (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                due_at REAL NOT NULL, PRIMARY KEY(chat_id,message_id))""")
            self.conn.commit()

    def _redis(self, *args):
        payload = json.dumps(list(args)).encode()
        req = urllib.request.Request(
            REDIS_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {REDIS_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        if data.get("error"):
            raise RuntimeError(data["error"])
        return data.get("result")

    def get_delay(self, chat_id):
        if self.redis:
            value = self._redis("GET", f"adbot:setting:{chat_id}")
            return int(value or 0)
        row = self.conn.execute(
            "SELECT delay_seconds FROM settings WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return int(row["delay_seconds"]) if row else 0

    def set_delay(self, chat_id, seconds):
        if self.redis:
            self._redis("SET", f"adbot:setting:{chat_id}", seconds)
        else:
            self.conn.execute(
                """INSERT INTO settings(chat_id,delay_seconds) VALUES(?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET delay_seconds=excluded.delay_seconds""",
                (chat_id, seconds),
            )
            self.conn.commit()

    def schedule(self, chat_id, message_id, delay):
        due = time.time() + delay
        member = f"{chat_id}:{message_id}"
        if self.redis:
            self._redis("ZADD", "adbot:scheduled", due, member)
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO scheduled VALUES(?,?,?)",
                (chat_id, message_id, due),
            )
            self.conn.commit()

    def due_messages(self, now):
        if self.redis:
            members = self._redis("ZRANGEBYSCORE", "adbot:scheduled", "-inf", now) or []
            rows = []
            for member in members[:100]:
                try:
                    chat_id, message_id = member.split(":", 1)
                    rows.append((int(chat_id), int(message_id)))
                except (ValueError, AttributeError):
                    self._redis("ZREM", "adbot:scheduled", member)
            return rows
        rows = self.conn.execute(
            "SELECT chat_id,message_id FROM scheduled WHERE due_at<=? ORDER BY due_at LIMIT 100",
            (now,),
        ).fetchall()
        return [(int(r["chat_id"]), int(r["message_id"])) for r in rows]

    def remove(self, chat_id, message_id):
        member = f"{chat_id}:{message_id}"
        if self.redis:
            self._redis("ZREM", "adbot:scheduled", member)
        else:
            self.conn.execute(
                "DELETE FROM scheduled WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
            self.conn.commit()

    def retry(self, chat_id, message_id):
        # Retry transient failures after 15 seconds.
        member = f"{chat_id}:{message_id}"
        due = time.time() + 15
        if self.redis:
            self._redis("ZADD", "adbot:scheduled", due, member)
        else:
            self.conn.execute(
                "UPDATE scheduled SET due_at=? WHERE chat_id=? AND message_id=?",
                (due, chat_id, message_id),
            )
            self.conn.commit()


store = Store()


async def reply_temp(update, text, context, seconds=15):
    msg = update.effective_message
    if not msg:
        return
    try:
        sent = await msg.reply_text(text)
        store.schedule(sent.chat_id, sent.message_id, seconds)
    except TelegramError as exc:
        logger.warning("Could not send reply: %s", exc)


async def is_admin(update, context):
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user or chat.type not in ("group", "supergroup"):
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


async def start(update, context):
    await reply_temp(
        update,
        "Hi! Add me to a group as an admin with Delete Messages permission.\\n"
        "An admin can use /settime 1 or /settime 2. Use /help for commands.",
        context,
    )


async def help_command(update, context):
    await reply_temp(
        update,
        "Commands (group admins only for settings):\\n"
        "/settime 1 — delete new messages after 1 minute\\n"
        "/settime 2 — delete after 2 minutes\\n"
        "/settime 10 — delete after 10 minutes\\n"
        "/off — turn auto-delete off\\n"
        "/time — show current timer\\n"
        "Allowed range: 1–1440 minutes. Timer is OFF by default.",
        context,
    )


async def settime(update, context):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await reply_temp(update, "Use this command inside a group.", context)
        return
    if not await is_admin(update, context):
        await reply_temp(update, "Only group admins can change this setting.", context)
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await reply_temp(update, "Usage: /settime 1  (minutes)", context)
        return
    minutes = int(context.args[0])
    if not 1 <= minutes <= 1440:
        await reply_temp(update, "Choose 1 to 1440 minutes.", context)
        return
    store.set_delay(chat.id, minutes * 60)
    await reply_temp(update, f"Auto-delete enabled: {minutes} minute(s).", context)


async def off(update, context):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await reply_temp(update, "Use this command inside a group.", context)
        return
    if not await is_admin(update, context):
        await reply_temp(update, "Only group admins can change this setting.", context)
        return
    store.set_delay(chat.id, 0)
    await reply_temp(update, "Auto-delete is now OFF.", context)


async def show_time(update, context):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await reply_temp(update, "Use this command inside a group.", context)
        return
    seconds = store.get_delay(chat.id)
    text = "Auto-delete is OFF." if not seconds else f"Current timer: {seconds // 60} minute(s)."
    await reply_temp(update, text, context)


async def on_message(update, context):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    delay = store.get_delay(chat.id)
    if delay > 0:
        store.schedule(chat.id, msg.message_id, delay)


async def deletion_worker(application):
    logger.info("Deletion scheduler started")
    while True:
        try:
            for chat_id, message_id in store.due_messages(time.time()):
                try:
                    await application.bot.delete_message(chat_id, message_id)
                    store.remove(chat_id, message_id)
                except (BadRequest, Forbidden) as exc:
                    logger.info("Message cannot be deleted %s/%s: %s", chat_id, message_id, exc)
                    store.remove(chat_id, message_id)
                except TelegramError as exc:
                    logger.warning("Temporary Telegram error: %s", exc)
                    store.retry(chat_id, message_id)
        except Exception:
            logger.exception("Scheduler/storage error")
        await asyncio.sleep(2)


async def post_init(application):
    application.bot_data["scheduler_task"] = asyncio.create_task(deletion_worker(application))


async def post_shutdown(application):
    task = application.bot_data.get("scheduler_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in Render Environment.")
    if not PUBLIC_URL:
        raise RuntimeError("Render did not provide RENDER_EXTERNAL_URL.")

    request = HTTPXRequest(
    connect_timeout=30,
    read_timeout=30,
    write_timeout=30,
    pool_timeout=30,
)

app = (
    Application.builder()
    .token(BOT_TOKEN)
    .request(request)
    .post_init(post_init)
    .post_shutdown(post_shutdown)
    .build()
)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settime", settime))
    app.add_handler(CommandHandler("off", off))
    app.add_handler(CommandHandler("time", show_time))
    app.add_handler(MessageHandler(filters.ALL, on_message), group=1)

    logger.info("Starting webhook service at %s", PUBLIC_URL)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{PUBLIC_URL}/{WEBHOOK_PATH}",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
