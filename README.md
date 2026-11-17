# XOXOZ AutoDelete + Ban Word Filter

## Features
- Auto-delete timer per group: `/settime 1`, `/settime 2`, `/off`, `/time`
- Admin-managed banned words/phrases: `/addword phrase`, `/delword phrase`, `/words`
- `/muteword on` mutes a non-admin, non-allowlisted member indefinitely when they use a configured word/phrase
- `/muteword off` disables that filter
- `/unmute USER_ID` restores a muted member
- `/banword on|off` remains as a compatibility alias for `/muteword`
- `/allowuser USER_ID` and `/unallowuser USER_ID` manage exemptions
- Case-insensitive matching; single words match whole words; phrases match as phrases
- Upstash Redis supported; SQLite fallback

## Deploy
Replace your GitHub repository files with this project, then Render → New → Blueprint → select repository. Set BOT_TOKEN to a newly generated BotFather token. Optional: configure Upstash Redis credentials for persistence.

## Telegram permissions
Make bot an administrator with both Delete Messages and Restrict Members (usually shown as Ban Users) permission.

## Important
Indefinite mutes can affect legitimate members on accidental matches. Add only clear prohibited words/phrases, test in a private group, and use `/delword` to remove mistakes. Filter is OFF by default; admins must run `/muteword on`. Admins and allowlisted users are exempt. The bot does not mute group admins/owners. Render Free may sleep, causing delayed deletions; local SQLite data can be lost on restarts, so configure Redis if persistence matters. Never commit tokens to GitHub.
