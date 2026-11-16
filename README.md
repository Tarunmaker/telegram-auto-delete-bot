# Telegram Auto Delete Bot — Free Render configuration

This version uses Telegram webhooks and a Render free web service (not a paid background worker).

## Deploy

1. Update your GitHub repository with these files.
2. In Render, choose New → Blueprint and select the repository.
3. Render reads `render.yaml` and creates a free web service.
4. In the service Environment tab, set `BOT_TOKEN` to the token from @BotFather.
5. Optional but recommended: create a free Upstash Redis database and set `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`. Without Redis, SQLite uses `/tmp` and can lose settings/schedules on restarts or redeploys.
6. Deploy and check Logs for `Starting webhook service`.

## Telegram setup

Add the bot to the group and promote it to administrator with Delete Messages permission. Make the bot a group administrator with Delete Messages permission. Telegram delivers group messages to bot administrators.

Commands:
- `/settime 1` — delete new messages after one minute
- `/settime 2` — delete after two minutes
- `/settime 10` — delete after ten minutes
- `/off` — disable
- `/time` — current setting
- `/help` — help

Only group admins can change settings. Auto-delete is OFF by default.

## Free-tier limitations — important

- Render free web services sleep after a period without inbound traffic. A Telegram webhook request wakes the service, but a sleeping service can delay processing.
- The deletion scheduler cannot run while the Render service is asleep. Therefore, a message's deletion may be late when the service has been idle; this is not guaranteed exact-time auto-delete.
- Use Upstash Redis variables to preserve settings and pending schedules across restarts. Without Redis, local SQLite is temporary on the free service.
- Free hosting is not guaranteed 24/7. For exact-time deletion, use an always-on host.
- Telegram restricts deletion of messages older than 48 hours. The bot needs Delete Messages administrator permission.
- Do not share the bot token or Redis token. Keep them in Render Environment variables, not GitHub.
