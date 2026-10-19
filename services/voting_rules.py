"""Swiss Bo1 matchdays vote and receive reminders as one batch."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from models import MatchStatus
from services.match_times import TOURNAMENT_TIMEZONE


def swiss_batch(data, match):
    if match.best_of != 1 or 'swiss' not in match.stage_id.lower():
        return [match]
    zone = ZoneInfo(TOURNAMENT_TIMEZONE)
    day = match.start_time.astimezone(zone).date()
    return sorted((m for m in data.matches if m.stage_id == match.stage_id and m.best_of == 1
                   and m.status != MatchStatus.CANCELLED and m.start_time.astimezone(zone).date() == day),
                  key=lambda m: (m.start_time, m.id)) or [match]


def voting_deadline(match, data=None):
    if data is not None:
        return swiss_batch(data, match)[0].start_time - timedelta(minutes=5)
    return getattr(match, 'voting_closes_at', None) or match.start_time - timedelta(minutes=5)


def batch_help(text):
    return text.replace('5 minutes before a match', '5 minutes before the first match of the Swiss Bo1 day')


def reminder_receipt_key(data, match):
    if len(swiss_batch(data, match)) == 1:
        return match.id
    day = match.start_time.astimezone(ZoneInfo(TOURNAMENT_TIMEZONE)).date().isoformat()
    return f'swiss-day:{match.tournament_id}:{day}'
