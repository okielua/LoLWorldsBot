FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BOT_MODE=production

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 worldsbot \
    && useradd --uid 10001 --gid worldsbot --no-create-home worldsbot \
    && mkdir -p /app/data/production \
    && chown worldsbot:worldsbot /app/data/production

COPY bot.py models.py ./
COPY services/ ./services/
COPY data/worlds_2026.json data/golgg_players_2026.json data/golgg_team_ids_2026.json data/champions.json ./data/
COPY deploy/backup_database.py ./deploy/backup_database.py

USER worldsbot
CMD ["python", "bot.py"]
