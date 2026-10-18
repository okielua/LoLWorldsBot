"""Explicit match times for every timezone registered through /mysetup."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import UTC, datetime, timedelta

from services.user_settings import get_registered_timezones

TOURNAMENT_TIMEZONE = "America/Los_Angeles"


def date_heading(when, timezone_name=TOURNAMENT_TIMEZONE, *, now=None, style="long"):
    zone = ZoneInfo(timezone_name)
    target = when.astimezone(zone).date() if isinstance(when, datetime) else when
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    if target == today:
        return "Today"
    if target == today + timedelta(days=1):
        return "Tomorrow"
    if style == "short":
        return f"{target.strftime('%a')}, {target.day} {target.strftime('%b %Y')}"
    if style == "weekday":
        return f"{target.strftime('%A')}, {target.day} {target.strftime('%B %Y')}"
    return f"{target.day} {target.strftime('%B %Y')}"


def format_clock_time(local, abbreviation=None, *, time_format="24h"):
    if time_format == "12h":
        text = f"{local.hour % 12 or 12}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"
    else:
        text = f"{local.hour}:{local.minute:02d}h"
    return f"{text} ({abbreviation})" if abbreviation else text


def format_tournament_date(when):
    local = when.astimezone(ZoneInfo(TOURNAMENT_TIMEZONE))
    return f"{local.strftime('%a')}, {local.day} {local.strftime('%b %Y')}"


def format_timezone_times(when, timezones, *, fallback="UTC"):
    if when.tzinfo is None:
        raise ValueError("Match time must be timezone-aware.")
    zones = set(timezones) or {fallback}
    official_date = when.astimezone(ZoneInfo(TOURNAMENT_TIMEZONE)).date()
    local_times = [(name, when.astimezone(ZoneInfo(name))) for name in zones]
    local_times.sort(key=lambda item: (item[1].utcoffset(), item[0]))
    return "\n".join(
        f"**{name.rsplit('/', 1)[-1].replace('_', ' ')}:** {format_clock_time(local)}"
        + (f" ({local.strftime('%a')}, {local.day} {local.strftime('%b %Y')})" if local.date() != official_date else "")
        for name, local in local_times
    )


def registered_match_times(when, timezones=None):
    return format_timezone_times(when, get_registered_timezones() if timezones is None else timezones)


def private_match_details(when, timezone_name=None, *, time_format="24h"):
    """Return the recipient's date and one local time; unset zones use PT."""
    if when.tzinfo is None:
        raise ValueError("Match time must be timezone-aware.")
    try:
        zone = ZoneInfo(timezone_name or TOURNAMENT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        timezone_name = None
        zone = ZoneInfo(TOURNAMENT_TIMEZONE)
    local = when.astimezone(zone)
    date = f"{local.strftime('%a')}, {local.day} {local.strftime('%b %Y')}"
    time = format_clock_time(local, (local.tzname() or timezone_name) if timezone_name else "PT", time_format=time_format)
    return date, time
