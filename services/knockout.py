"""Shared knockout scoring and publication rules."""
from datetime import datetime, time
from zoneinfo import ZoneInfo


def is_knockout(match):
    return bool(match and match.best_of == 5 and any(s in match.stage_id.lower() for s in ('quarterfinal', 'semifinal', 'final')) and 'swiss' not in match.stage_id.lower())


def prediction_points(match, chosen, losing_games=None):
    if chosen != match.winner_id:
        return 0
    if not is_knockout(match) or losing_games is None:
        return 1
    if losing_games not in (0, 1, 2):
        raise ValueError('Choose a 3–0, 3–1 or 3–2 prediction.')
    actual = min(match.team1_score, match.team2_score)
    return 3 - abs(losing_games - actual)


def publication_time(match):
    berlin = ZoneInfo('Europe/Berlin')
    return datetime.combine(match.start_time.astimezone(berlin).date(), time(4), berlin)
