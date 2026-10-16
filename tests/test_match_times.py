from datetime import UTC, datetime

import pytest

from services.match_times import format_timezone_times, private_match_details


def test_registered_city_times_include_midnight_date_change_and_no_seconds():
    when = datetime(2026, 10, 15, 18, tzinfo=UTC)
    text = format_timezone_times(when, ["Asia/Tokyo", "Europe/London", "Europe/Berlin", "Europe/Berlin"])
    assert text.splitlines() == [
        "**London:** 19:00h",
        "**Berlin:** 20:00h",
        "**Tokyo:** 3:00h (Fri, 16 Oct 2026)",
    ]
    assert "(in " not in text


def test_match_date_controls_daylight_saving_not_current_date():
    text = format_timezone_times(datetime(2026, 11, 1, 18, tzinfo=UTC), ["Europe/London", "Europe/Berlin"])
    assert "**London:** 18:00h" in text
    assert "**Berlin:** 19:00h" in text
    assert "BST" not in text and "CEST" not in text


def test_no_setup_zones_uses_explicit_fallback():
    assert "**Berlin:** 20:00h" in format_timezone_times(datetime(2026, 10, 15, 18, tzinfo=UTC), [], fallback="Europe/Berlin")
    with pytest.raises(ValueError):
        format_timezone_times(datetime(2026, 10, 15), ["Europe/Berlin"])


def test_city_date_suffix_compares_with_riot_day_rather_than_utc_day():
    text = format_timezone_times(datetime(2026, 10, 16, 0, tzinfo=UTC), ["America/Los_Angeles", "Europe/London"])
    assert text.splitlines() == ["**Los Angeles:** 17:00h", "**London:** 1:00h (Fri, 16 Oct 2026)"]


@pytest.mark.parametrize("zone,date,time", [
    ("Europe/Berlin", "Thu, 15 Oct 2026", "20:00h (CEST)"),
    ("Asia/Seoul", "Fri, 16 Oct 2026", "3:00h (KST)"),
    (None, "Thu, 15 Oct 2026", "11:00h (PT)"),
    ("Invalid/Timezone", "Thu, 15 Oct 2026", "11:00h (PT)"),
])
def test_private_time_uses_only_recipient_timezone_or_pacific(zone, date, time):
    assert private_match_details(datetime(2026, 10, 15, 18, tzinfo=UTC), zone) == (date, time)


def test_private_pacific_fallback_observes_daylight_saving_and_rejects_naive_time():
    assert private_match_details(datetime(2026, 12, 1, 18, tzinfo=UTC)) == ("Tue, 1 Dec 2026", "10:00h (PT)")
    with pytest.raises(ValueError):
        private_match_details(datetime(2026, 10, 15, 18))


@pytest.mark.parametrize("hour,expected", [(0, "12:05 am (UTC)"), (1, "1:05 am (UTC)"),
                                         (12, "12:05 pm (UTC)"), (23, "11:05 pm (UTC)")])
def test_twelve_hour_time_handles_midnight_noon_and_minutes(hour, expected):
    assert private_match_details(datetime(2026, 10, 15, hour, 5, tzinfo=UTC), "UTC", time_format="12h")[1] == expected


def test_twelve_hour_time_uses_personal_zone_and_pt_fallback():
    when = datetime(2026, 10, 15, 23, tzinfo=UTC)
    assert private_match_details(when, "Europe/Berlin", time_format="12h") == ("Fri, 16 Oct 2026", "1:00 am (CEST)")
    assert private_match_details(when, time_format="12h") == ("Thu, 15 Oct 2026", "4:00 pm (PT)")
    assert format_timezone_times(when, ["Europe/Berlin"]) == "**Berlin:** 1:00h (Fri, 16 Oct 2026)"
