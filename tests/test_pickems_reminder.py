import asyncio
from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest

from services.pickems_reminder import PickemsReminder
from services.server_settings import ServerSettings
from test_voting import context


def build(context, *, offset=timedelta(hours=8), enabled=True, role=999):
    manager, store, channel, current, _ = context
    match = replace(store.data.matches[0], stage_id="worlds-2026-swiss")
    store.data = replace(store.data, matches=[match])
    current[0] = match.start_time - offset
    store.fetched_at = current[0]
    settings = ServerSettings(123, 456, enabled, worlds_role_id=role)
    reminder = PickemsReminder(manager.client, store, 123, clock=lambda: current[0], load_settings=lambda _: settings)
    return reminder, channel, current, store


def test_one_role_ping_at_eight_hours_is_persisted_across_restarts(context):
    reminder, channel, _, _ = build(context)

    async def run():
        assert await reminder.tick()
        assert not await reminder.tick()
        restarted = PickemsReminder(reminder.client, reminder.store, reminder.guild_id,
                                    clock=reminder.clock, load_settings=reminder.load_settings)
        assert not await restarted.tick()
        channel.send.assert_awaited_once()
        kwargs = channel.send.call_args.kwargs
        text = str(kwargs["view"].to_components())
        assert "8 hours and 0 minutes" in text and "<@&999>" in text
        assert "https://lolesports.com/en-GB/pickems" in text
        assert kwargs["allowed_mentions"].to_dict() == {"roles": [999], "parse": []}

    asyncio.run(run())


@pytest.mark.parametrize("offset,enabled,role,expected", [
    (timedelta(hours=8, seconds=1), True, 999, False),
    (timedelta(hours=7, minutes=23), True, 999, True),
    (timedelta(0), True, 999, False),
    (-timedelta(seconds=1), True, 999, False),
    (timedelta(hours=8), False, 999, False),
    (timedelta(hours=8), True, None, False),
])
def test_due_window_and_configuration(context, offset, enabled, role, expected):
    reminder, channel, _, _ = build(context, offset=offset, enabled=enabled, role=role)
    assert asyncio.run(reminder.tick()) is expected
    assert channel.send.await_count == int(expected)
    if expected:
        assert "7 hours and 23 minutes" in str(channel.send.call_args.kwargs["view"].to_components())


def test_stale_or_missing_swiss_data_does_not_guess_a_deadline(context):
    reminder, channel, current, store = build(context)
    store.fetched_at -= timedelta(minutes=6)
    assert not asyncio.run(reminder.tick())
    store.fetched_at = current[0]
    store.data = replace(store.data, matches=[])
    assert not asyncio.run(reminder.tick())
    channel.send.assert_not_awaited()


def test_failed_delivery_retries_without_consuming_receipt(context):
    reminder, channel, _, _ = build(context)
    channel.send.side_effect = [OSError("network error"), type("Message", (), {"id": 44})()]

    async def run():
        with pytest.raises(OSError):
            await reminder.tick()
        assert await reminder.tick()
        assert not await reminder.tick()
        assert channel.send.await_count == 2

    asyncio.run(run())


def test_successful_send_with_receipt_write_failure_is_not_resent(context, monkeypatch):
    reminder, channel, _, _ = build(context)
    original = reminder._receipt

    def fail_write(tournament_id, message=None):
        if message is not None:
            raise sqlite3.OperationalError("database locked")
        return original(tournament_id)

    async def run():
        monkeypatch.setattr(reminder, "_receipt", fail_write)
        with pytest.raises(sqlite3.OperationalError):
            await reminder.tick()
        monkeypatch.setattr(reminder, "_receipt", original)
        assert not await reminder.tick()
        channel.send.assert_awaited_once()

    asyncio.run(run())
