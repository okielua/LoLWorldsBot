import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from models import MatchStatus
from services.live_tournament import LiveTournamentStore, normalize_events
from services.lolesports import LoLEsportsClient, SourceError
from services.reminder_engine import ReminderTracker, UserReminderPreferences
from services.reminder_scheduler import check_reminders
from services.tournament_data import load_tournament_data


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def seed():
    return load_tournament_data(ROOT / "data" / "worlds_2026.json")


@pytest.fixture
def events():
    # Trimmed public response captured from the site on 14 October 2026.
    return json.loads((ROOT / "tests" / "fixtures" / "lolesports_events.json").read_text())


def test_normalize_preserves_follow_ids_and_uses_source_match_ids(seed, events):
    data = normalize_events(events, seed)
    match = data.matches[0]
    assert (match.team1_id, match.team2_id) == ("C9", "KC")
    assert match.id == events[0]["match"]["id"]
    assert match.start_time == datetime(2026, 10, 15, 18, tzinfo=UTC)
    assert match.round_name == "Opening Round"
    assert match.status == MatchStatus.SCHEDULED
    assert match.team1_score is None
    assert set(seed.teams_by_id) <= set(data.teams_by_id)


@pytest.mark.parametrize("state", ["inProgress", "completed"])
def test_scores_and_winner_are_normalized(seed, events, state):
    event = events[0]
    event["state"] = event["match"]["state"] = state
    for team, wins, outcome in zip(event["matchTeams"], [3 if state == "completed" else 1, 1], ["win", "loss"]):
        team["result"] = {"gameWins": wins, "outcome": outcome if state == "completed" else None}

    match = normalize_events(events, seed).matches[0]

    assert match.status == (MatchStatus.LIVE if state == "inProgress" else MatchStatus.COMPLETED)
    assert match.team1_score == (1 if state == "inProgress" else 3)
    assert match.team2_score == 1
    assert match.winner_id == ("C9" if state == "completed" else None)


def test_tbd_slots_are_displayable_but_never_remind(seed, events):
    data = normalize_events(events, seed)
    match = data.matches[2]
    assert data.teams_by_id[match.team1_id].short_name == "TBD"
    user = UserReminderPreferences(1, frozenset({"C9"}), all_games_server_ping=True)
    from datetime import timedelta
    result = check_reminders([match], [user], match.start_time - timedelta(minutes=5), ReminderTracker())
    assert result.server_reminders == ()
    assert result.dm_reminders == ()


@pytest.mark.parametrize("mutation", [
    lambda e: e[0].update(state="unknown"),
    lambda e: e[0].update(startTime="2026-10-15T18:00:00"),
    lambda e: e[0].update(startTime="2027-01-01T18:00:00Z"),
    lambda e: e[0].update(blockName="Unknown stage"),
    lambda e: e.append(deepcopy(e[0])),
    lambda e: e[0]["match"].update(id="different"),
    lambda e: e[0]["match"].update(state="completed"),
    lambda e: e[0]["match"]["strategy"].update(count=2),
    lambda e: e[0].update(matchTeams=[]),
    lambda e: e[0]["matchTeams"][0]["result"].update(gameWins=-1),
    lambda e: e[0]["matchTeams"][0]["result"].update(outcome="unknown"),
])
def test_bad_snapshot_is_rejected(seed, events, mutation):
    mutation(events)
    with pytest.raises(SourceError):
        normalize_events(events, seed)


def test_other_tournaments_are_not_imported(seed, events):
    unrelated = deepcopy(events[0])
    unrelated["tournament"]["id"] = "another-tournament"
    assert len(normalize_events([*events, unrelated], seed).matches) == 3
    with pytest.raises(SourceError):
        normalize_events([unrelated], seed)


def test_refresh_cache_survives_restart_and_network_failure(seed, events, tmp_path):
    client = AsyncMock()
    client.fetch_events.return_value = events
    path = tmp_path / "cache.json"
    store = LiveTournamentStore(seed, path, client)
    assert asyncio.run(store.refresh()) is True
    before = path.read_bytes()
    restarted = LiveTournamentStore(seed, path, client)
    assert restarted.source == "cache"
    assert restarted.data == store.data
    assert restarted.fetched_at == store.fetched_at
    client.fetch_events.side_effect = TimeoutError("offline")
    assert asyncio.run(restarted.refresh()) is False
    assert restarted.data == store.data
    assert path.read_bytes() == before
    assert restarted.last_error == "offline"


def test_invalid_refresh_preserves_last_good_snapshot(seed, events, tmp_path):
    client = AsyncMock()
    client.fetch_events.return_value = events
    path = tmp_path / "cache.json"
    store = LiveTournamentStore(seed, path, client)
    assert asyncio.run(store.refresh())
    previous = store.data
    before = path.read_bytes()
    client.fetch_events.return_value = []
    assert not asyncio.run(store.refresh())
    assert store.data is previous
    assert path.read_bytes() == before


def test_corrupt_cache_and_first_fetch_failure_use_seed(seed, tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("not json")
    client = AsyncMock()
    client.fetch_events.side_effect = OSError("offline")
    store = LiveTournamentStore(seed, path, client)
    assert store.source == "seed"
    assert not asyncio.run(store.refresh())
    assert store.data is seed


def test_failed_cache_write_preserves_previous_snapshot(seed, events, tmp_path, monkeypatch):
    client = AsyncMock()
    client.fetch_events.return_value = events
    store = LiveTournamentStore(seed, tmp_path / "cache.json", client)
    def fail_replace(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(Path, "replace", fail_replace)
    assert not asyncio.run(store.refresh())
    assert store.data is seed
    assert store.fetched_at is None


def test_large_schedule_drop_retains_previous_cache(seed, events, tmp_path):
    client = AsyncMock()
    client.fetch_events.return_value = events
    path = tmp_path / "cache.json"
    store = LiveTournamentStore(seed, path, client)
    assert asyncio.run(store.refresh())
    before = path.read_bytes()
    client.fetch_events.return_value = events[:1]
    assert not asyncio.run(store.refresh())
    assert len(store.data.matches) == 3
    assert path.read_bytes() == before


@pytest.mark.parametrize("status", [401, 403, 429])
def test_source_access_errors_and_rate_limits_preserve_data(seed, tmp_path, status):
    import aiohttp
    from types import SimpleNamespace
    client = AsyncMock()
    client.fetch_events.side_effect = aiohttp.ClientResponseError(
        SimpleNamespace(real_url="https://lolesports.com/api/gql"), (),
        status=status, message="source unavailable", headers={"Retry-After": "120"},
    )
    store = LiveTournamentStore(seed, tmp_path / "cache.json", client)
    assert not asyncio.run(store.refresh())
    assert store.data is seed
    if status == 429:
        assert 115 <= (store.retry_not_before - datetime.now(UTC)).total_seconds() <= 120
    else:
        assert "LOLESPORTS_CLIENT_VERSION" in store.last_error


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    async def json(self):
        return self.payload


class FakeSession(FakeResponse):
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    def get(self, url, params):
        self.requests.append(json.loads(params["variables"]))
        return FakeResponse(next(self.pages))


def page(events, newer=None):
    return {"data": {"esports": {"events": events, "pages": {"newer": newer}}}}


def test_client_fetches_all_pages_and_includes_all_statuses(events, monkeypatch):
    session = FakeSession([page(events[:2], "next"), page(events[2:])])
    captured = {}
    def factory(**kwargs):
        captured.update(kwargs)
        return session
    monkeypatch.setattr("services.lolesports.aiohttp.ClientSession", factory)
    result = asyncio.run(LoLEsportsClient().fetch_events(
        datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
    ))
    assert result == events
    assert session.requests[1]["pageToken"] == "next"
    assert session.requests[0]["eventState"] == ["unstarted", "inProgress", "completed"]
    assert captured["headers"]["apollographql-client-name"] == "Esports Web"
    assert "Cookie" not in captured["headers"]


@pytest.mark.parametrize("pages", [
    [{"errors": [{"message": "unavailable"}]}],
    [{"data": {"esports": {"events": []}}}],
    [page([], "same"), page([], "same")],
    [page([{"id": "1"}], "next"), page([{"id": "1"}])],
])
def test_incomplete_or_repeating_pagination_is_rejected(pages, monkeypatch):
    session = FakeSession(pages)
    monkeypatch.setattr("services.lolesports.aiohttp.ClientSession", lambda **kwargs: session)
    with pytest.raises(SourceError):
        asyncio.run(LoLEsportsClient().fetch_events(
            datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
        ))
