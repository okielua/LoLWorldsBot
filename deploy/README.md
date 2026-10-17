# Production Deployment

The local development bot remains available. No server has been deployed by
adding these files. Keep the development and production bots' tokens separate.

## Discord

Create a second application for the live bot in the Discord Developer Portal.
Invite it to the live server with the `bot` and `applications.commands` scopes.
Grant View Channel, Send Messages, Read Message History, Embed Links, and Use
External Emojis where needed. For self-service role subscriptions, grant Manage
Roles and put the bot's role above the notification role. Make the notification
role mentionable if the bot cannot mention all roles.

Keep tokens private. Copy `.env.production.example` to `.env.production`, fill
in the live token, server ID, and live application ID. Production checks that the
token belongs to that application before syncing commands or posting anything.

## Files and Runtime

Use a dedicated `worldsbot` Linux user and install the project at `/opt/worldsbot`.
Create a Python 3.11 virtual environment there and install `requirements.txt`.
Copy source code, requirements, and committed data metadata. Do not copy `.env`,
`.venv`, `.logs`, or the development database/cache from your Windows PC.
The existing development `.env` and `data/worldsbot.db` stay unchanged locally.

Production automatically uses `data/production/worldsbot.db` and its own schedule
cache. It never reads development votes or the development schedule cache.
Make `/opt/worldsbot/data/production` writable by `worldsbot` before starting the
service; keep `.env.production` readable only by that user. The bundled sample
matches and standings are removed from the production seed. Verified cached
schedule data remains available if a later source request fails.

Install `deploy/worldsbot.service` as `/etc/systemd/system/worldsbot.service`,
run `systemctl daemon-reload`, then `systemctl enable --now worldsbot`.
Inspect startup using `journalctl -u worldsbot -n 50`. Confirm command sync,
successful live schedule refresh, and bot login before enabling public reminders.
The service starts on reboot and restarts after a crash.

## First Live Setup

Use `/serversetup` with the Admin role to select the intended channel, notification
role and Pick'ems link. New production settings start with public reminders Off.
Check `/today`, `/team`, `/mysetup`, and `/help` before turning reminders On.
Turning reminders On can immediately publish the eligible matchday's voting post.
Use the separate development bot for all simulated checks. Production does not
register test commands, and its setup panel contains no test/preview controls.

The two bot applications must not both post into the live channel. Keep the
development bot's configured reminder channel in the testing server/channel.
No development votes are migrated automatically; any real votes that need to be
preserved must be reviewed and migrated deliberately before enabling production.

## Backups

Use SQLite's backup API while the bot is running:

```sh
/opt/worldsbot/.venv/bin/python /opt/worldsbot/deploy/backup_database.py /opt/worldsbot/data/production/worldsbot.db /var/backups/worldsbot
```

Create the destination with access restricted to the backup user. Schedule this
daily with the VPS's scheduler and retain a copy outside the VPS. Database backups
contain member preferences and vote history. Restore only with the bot stopped.
