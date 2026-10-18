from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from models import Match, MatchStatus, Team
from services.spoilers import SpoilerPolicy, padded_name


def scenario(next_start=None):
    end = datetime(2026, 10, 18, 20, tzinfo=UTC)
    earlier = Match("prior", "worlds", "playin", "KC", "MVK", end - timedelta(hours=4), 5,
                    status=MatchStatus.COMPLETED, team1_score=3, team2_score=1, winner_id="KC")
    matches = [earlier]
    if next_start:
        matches.append(Match("next", "worlds", "swiss", "KC", "T1", next_start, 3))
    teams = [Team(code, code, code) for code in ("KC", "MVK", "T1")]
    data = SimpleNamespace(matches=matches, matches_by_id={m.id: m for m in matches}, teams=teams,
                           teams_by_id={team.id: team for team in teams})
    return end, earlier, data


def test_centering_uses_nbsp_and_extra_space_on_right():
    assert padded_name("T1", 5) == "\xa0T1\xa0\xa0"
    assert padded_name("HLE", 4) == "HLE\xa0"
    assert padded_name("LONGER", 4) == "LONGER"


def test_shared_field_keeps_both_margins_even_for_longest_tag():
    _, _, data = scenario()
    data.teams.append(Team("TLAW", "Team Liquid", "TLAW"))
    policy = SpoilerPolicy(data)
    assert policy.width == 6
    for name in ("KC", "MVK", "TLAW", "TBD"):
        field = padded_name(name, policy.width)
        assert len(field) == policy.width
        assert field.startswith("\xa0") and field.endswith("\xa0")
    assert padded_name("MVK", policy.width) == "\xa0MVK\xa0\xa0"


def test_full_series_score_releases_at_exact_fourteen_hour_boundary():
    end, earlier, data = scenario()
    policy = SpoilerPolicy(data, ended_at={earlier.id: end})
    assert policy.score(earlier, end + timedelta(hours=14, microseconds=-1)) == "||3–1||"
    assert policy.score(earlier, end + timedelta(hours=14)) == "3–1"


def test_earliest_connected_match_shortens_window_and_cancelled_does_not():
    end, earlier, data = scenario(datetime(2026, 10, 19, 5, tzinfo=UTC))
    policy = SpoilerPolicy(data, ended_at={earlier.id: end})
    assert policy.release_at(earlier) == end + timedelta(hours=7)
    data.matches.append(replace(data.matches[-1], id="cancelled", start_time=end + timedelta(hours=1), status=MatchStatus.CANCELLED))
    assert policy.release_at(earlier) == end + timedelta(hours=7)


def test_dependency_connects_unknown_fixture_and_slots_are_independent():
    end, earlier, data = scenario(datetime(2026, 10, 19, 5, tzinfo=UTC))
    future = replace(data.matches[-1], team1_id="T1", team2_id="TBD")
    data.matches[-1] = future
    data.matches_by_id[future.id] = future
    policy = SpoilerPolicy(data, ended_at={earlier.id: end}, dependencies={(future.id, 2): [earlier.id]})
    assert policy.release_at(earlier) == end + timedelta(hours=7)
    assert not policy.participant_protected(future, 1, end)
    assert policy.participant_protected(future, 2, end)
    assert policy.team("T1", match=future, slot=1, emoji=":t1:", now=end) == ":t1: `\xa0\xa0T1\xa0\xa0`"
    assert policy.team("MVK", match=future, slot=2, emoji=":mvk:", now=end) == "||:mvk: `\xa0MVK\xa0\xa0`||"
    assert not policy.participant_protected(future, 2, end + timedelta(hours=7))


def test_live_scores_never_render_and_unknown_end_fails_closed():
    end, earlier, data = scenario()
    policy = SpoilerPolicy(data)
    assert policy.protected(earlier, end + timedelta(days=1))
    assert policy.score(replace(earlier, status=MatchStatus.LIVE), end) is None
