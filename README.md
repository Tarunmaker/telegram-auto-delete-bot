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
