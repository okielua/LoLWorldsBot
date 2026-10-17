# Production Deployment

The local development bot remains available. No server has been deployed by
adding these files. Keep the development and production bots' tokens separate.

## Docker on the VPS

Docker Compose is the preferred route when Docker is already installed. Use
either Docker or the systemd service below, so only one live bot runs.

After logging in to the VPS with SSH:

```sh
docker compose version
apt update
apt install -y git nano
systemctl enable --now docker
git clone https://github.com/okielua/LoLWorldsBot.git /opt/worldsbot
cd /opt/worldsbot
cp .env.production.example .env.production
chmod 600 .env.production
nano .env.production
```

Enter the LIVE bot token and your Discord server ID. The live Application ID is
already filled in. Save with Ctrl+O, Enter; exit with Ctrl+X. The token belongs
only in this private VPS file, not in Git or chat. For a private repository,
use a read-only GitHub deploy key; a GitHub account password cannot authenticate
Git HTTPS cloning. Never put an access token into the clone URL.

Build and start:

```sh
docker compose up -d --build
docker compose logs --tail=60 -f bot
```

Look for a successful live schedule refresh, command sync, and Discord login.
Ctrl+C exits the log viewer while the bot continues running. New production
settings keep public reminders Off; complete the live setup below before
enabling them. No incoming network ports need to be exposed for this bot.

The named `production-data` volume holds the database and schedule cache across
container rebuilds/restarts. Do not run `docker compose down -v`; it deletes the
volume and vote history. Docker restarts the bot after crashes and host reboots
unless you explicitly stop it.

To update later, back up first, then run `git pull --ff-only` and
`docker compose up -d --build`. A GitHub push does not automatically update the
VPS. For a database backup inside the persistent volume:

```sh
docker compose exec -T bot python deploy/backup_database.py /app/data/production/worldsbot.db /app/data/production/backups
```

The command prints the backup path. Copy it outside the VPS as well, for example
using `docker compose cp bot:/app/data/production/backups ./backups`, then SCP to
your PC. Schedule backups daily after the bot has created its database.

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
