import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from models import MatchStatus
from services.background_polling import BackgroundPolling
from services.reminder_engine import ReminderKey, ReminderTracker, ReminderType, UserReminderPreferences
from services.tournament_data import load_tournament_data


NOW = datetime(2026, 10, 15, 17, 55, tzinfo=UTC)


def make_poller():
    seed = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    store = SimpleNamespace(
        data=seed, fetched_at=NOW, last_error=None, refresh=AsyncMock(return_value=True)
    )
    users = Mock(return_value=[UserReminderPreferences(
        user_id=123, followed_team_ids=frozenset({"C9"}),
        dm_enabled=True, dm_reminder_minutes=0,
    )])
    publish = Mock()
    poller = BackgroundPolling(store, users, publish, AsyncMock(), clock=lambda: NOW)
    return poller, store, users, publish


def test_preview_checks_current_preferences_and_deduplicates_without_delivery():
    poller, store, users, _ = make_poller()
    first = asyncio.run(poller.preview_once())
    assert first.server_reminders[0].ping_user_ids == frozenset({123})
    assert asyncio.run(poller.preview_once()).server_reminders == ()
    assert users.call_count == 2
    poller.clock = lambda: NOW + timedelta(minutes=5)
    store.fetched_at = poller.clock()
    assert asyncio.run(poller.preview_once()).dm_reminders == ()


@pytest.mark.parametrize("age, error", [
    (None, None), (timedelta(minutes=6), None),
    (timedelta(seconds=-1), None), (timedelta(seconds=0), "offline"),
])
def test_stale_or_failed_data_pauses_previews_without_consuming_keys(age, error):
    poller, store, users, _ = make_poller()
    store.fetched_at = None if age is None else NOW - age
    store.last_error = error
    assert asyncio.run(poller.preview_once()).server_reminders == ()
    users.assert_not_called()
    store.fetched_at, store.last_error = NOW, None
    assert len(asyncio.run(poller.preview_once()).server_reminders) == 1


def test_successful_source_refresh_wakes_prediction_loop_without_waiting_for_timer():
    poller, _, _, _ = make_poller()
    async def scenario():
        first = asyncio.Event()
        second = asyncio.Event()
        calls = [0]
        async def tick():
            calls[0] += 1
            (first if calls[0] == 1 else second).set()
        poller.voting_manager = SimpleNamespace(tick=tick)
        task = asyncio.create_task(poller._poll_loop())
        try:
            await asyncio.wait_for(first.wait(), timeout=1)
            assert await poller.refresh_once()
            await asyncio.wait_for(second.wait(), timeout=1)
            assert calls[0] == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_failed_refresh_during_database_read_does_not_generate_preview(monkeypatch):
    poller, store, _, _ = make_poller()
    async def load(*args):
        store.last_error = "offline"
        return []
    monkeypatch.setattr("services.background_polling.asyncio.to_thread", load)
    assert asyncio.run(poller.preview_once()).server_reminders == ()


@pytest.mark.parametrize("status, offset, expected", [
    (MatchStatus.SCHEDULED, 120, 300),
    (MatchStatus.SCHEDULED, 60, 60),
    (MatchStatus.SCHEDULED, -30, 60),
    (MatchStatus.LIVE, -120, 60),
    (MatchStatus.COMPLETED, 30, 300),
    (MatchStatus.CANCELLED, 30, 300),
])
def test_source_polling_cadence(status, offset, expected):
    poller, store, _, _ = make_poller()
    store.data = replace(store.data, matches=[replace(
        store.data.matches[0], status=status, start_time=NOW + timedelta(minutes=offset)
    )])
    assert poller.refresh_interval_seconds() == expected


def test_failed_refresh_backs_off_and_success_publishes_latest_data():
    poller, store, _, publish = make_poller()
    poller.clock = lambda: NOW - timedelta(hours=2)
    store.refresh.return_value = False
    delays = []
    for _ in range(6):
        assert not asyncio.run(poller.refresh_once())
        delays.append(poller.refresh_interval_seconds())
    assert delays == [60, 120, 240, 480, 900, 900]
    publish.assert_not_called()
    store.refresh.return_value = True
    assert asyncio.run(poller.refresh_once())
    assert poller.failures == 0
    publish.assert_called_once_with(store.data)


def test_unexpected_source_error_is_retried():
    poller, store, _, _ = make_poller()
    store.refresh.side_effect = RuntimeError("unexpected source error")
    assert not asyncio.run(poller.refresh_once())
    assert store.last_error == "unexpected source error"
    assert poller.failures == 1


def test_changed_match_rearms_preview_without_clearing_other_matches():
    poller, store, _, _ = make_poller()
    first, second = store.data.matches
    first_key = ReminderKey(first.id, ReminderType.SERVER)
    second_key = ReminderKey(second.id, ReminderType.SERVER)
    poller.preview_tracker.mark_sent(first_key)
    poller.preview_tracker.mark_sent(second_key)
    async def refresh():
        store.data = replace(store.data, matches=[replace(first, start_time=first.start_time + timedelta(hours=1)), second])
        return True
    store.refresh.side_effect = refresh
    assert asyncio.run(poller.refresh_once())
    assert not poller.preview_tracker.has_sent(first_key)
    assert poller.preview_tracker.has_sent(second_key)


def test_polling_waits_for_ready_starts_once_and_cancels_cleanly():
    async def scenario():
        poller, store, _, _ = make_poller()
        ready, previewed = asyncio.Event(), asyncio.Event()
        poller.wait_until_ready = ready.wait
        async def preview():
            previewed.set()
        poller.preview_once = AsyncMock(side_effect=preview)
        poller.start()
        tasks = dict(poller._tasks)
        poller.start()
        assert poller._tasks == tasks
        await asyncio.sleep(0)
        poller.preview_once.assert_not_awaited()
        ready.set()
        await asyncio.wait_for(previewed.wait(), timeout=1)
        await poller.stop()
        assert all(task.done() for task in tasks.values())
        assert not poller._tasks
        store.refresh.assert_not_awaited()  # Initial refresh happens in setup_hook.
    asyncio.run(scenario())


def test_preview_loop_survives_an_iteration_error(monkeypatch):
    monkeypatch.setattr("services.background_polling.REMINDER_INTERVAL_SECONDS", 0)
    async def scenario():
        poller, _, _, _ = make_poller()
        recovered = asyncio.Event()
        attempts = 0
        async def preview():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("database temporarily unavailable")
            recovered.set()
        poller.preview_once = preview
        poller.start()
        try:
            await asyncio.wait_for(recovered.wait(), timeout=1)
            assert attempts >= 2
        finally:
            await poller.stop()
    asyncio.run(scenario())


def test_shutdown_cancels_an_inflight_source_request():
    async def scenario():
        poller, store, _, _ = make_poller()
        entered, blocked = asyncio.Event(), asyncio.Event()
        async def refresh():
            entered.set()
            await blocked.wait()
        store.refresh.side_effect = refresh
        poller.refresh_interval_seconds = lambda: 0
        poller.start()
        tasks = dict(poller._tasks)
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
        finally:
            await asyncio.wait_for(poller.stop(), timeout=1)
        assert tasks["source-refresh"].cancelled()
        assert not poller._tasks
    asyncio.run(scenario())


def test_polling_retries_failed_public_send_separately_from_preview_keys():
    poller, store, _, _ = make_poller()
    tracker = ReminderTracker()
    attempts = 0
    async def deliver(match_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        tracker.mark_sent(ReminderKey(match_id, ReminderType.SERVER))
        return True
    delivery = SimpleNamespace(
        tracker=tracker, deliver=AsyncMock(side_effect=deliver), flush_pending_records=AsyncMock()
    )
    poller.server_delivery = delivery
    for _ in range(3):
        asyncio.run(poller.preview_once())
    assert delivery.deliver.await_count == 2
    assert tracker.has_sent(ReminderKey(store.data.matches[0].id, ReminderType.SERVER))


def test_receipt_writes_retry_even_when_stale_data_pauses_reminders():
    poller, store, _, _ = make_poller()
    store.last_error = "offline"
    delivery = SimpleNamespace(flush_pending_records=AsyncMock(), deliver=AsyncMock())
    poller.server_delivery = delivery
    assert asyncio.run(poller.preview_once()).server_reminders == ()
    delivery.flush_pending_records.assert_awaited_once()
    delivery.deliver.assert_not_awaited()
    asyncio.run(poller.stop())
    assert delivery.flush_pending_records.await_count == 2


def test_source_failures_retry_each_minute_near_matches_but_honor_rate_limit():
    poller, store, _, _ = make_poller()
    poller.failures = 6
    assert poller.refresh_interval_seconds() == 60
    store.retry_not_before = NOW + timedelta(seconds=180)
    assert poller.refresh_interval_seconds() == 180


def test_refresh_recovery_catches_up_missed_public_reminder():
    poller, store, _, _ = make_poller()
    current = NOW + timedelta(minutes=2)
    poller.clock = lambda: current
    store.last_error = "offline"
    tracker = ReminderTracker()
    async def deliver(match_id):
        tracker.mark_sent(ReminderKey(match_id, ReminderType.SERVER))
        return True
    delivery = SimpleNamespace(
        tracker=tracker, deliver=AsyncMock(side_effect=deliver), flush_pending_records=AsyncMock()
    )
    poller.server_delivery = delivery
    asyncio.run(poller.preview_once())
    delivery.deliver.assert_not_awaited()
    async def refresh():
        store.fetched_at = current
        store.last_error = None
        return True
    store.refresh.side_effect = refresh
    assert asyncio.run(poller.refresh_once())
    asyncio.run(poller.preview_once())
    delivery.deliver.assert_awaited_once_with(store.data.matches[0].id)
    asyncio.run(poller.preview_once())
    delivery.deliver.assert_awaited_once()
