from presentation_assertions import rendered_message, rendered_payload
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from models import MatchStatus
from services import user_settings
from services.delivery_records import DeliveryRecord, get_delivery_record, initialize_delivery_records, record_delivery
from services.server_reminders import ServerReminderDelivery
from services.server_settings import (
    ServerSettings, get_server_settings, initialize_server_settings,
    set_public_reminders_enabled, set_reminder_channel,
    set_pickems_url,
)
from services.server_setup import PickemsLinkModal, ServerSetupView, build_server_setup_content
from services.reminder_test import run_reminder_test
from services.reminder_engine import ReminderKey, ReminderType, UserReminderPreferences
from services.tournament_data import load_tournament_data


NOW = datetime(2026, 10, 15, 17, 55, tzinfo=UTC)


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "settings.db")
    user_settings.initialize_database()
    user_settings.mark_setup_completed(123, 7)
    initialize_server_settings()
    initialize_delivery_records()


def make_delivery():
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    store = SimpleNamespace(data=data, fetched_at=NOW, last_error=None)
    channel = Mock(spec=discord.TextChannel)
    channel.id = 456
    channel.type = discord.ChannelType.text
    channel.guild = SimpleNamespace(id=123, me=object(), get_member=lambda user_id: SimpleNamespace(roles=[]))
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=9876))
    client = SimpleNamespace(get_channel=Mock(return_value=channel), fetch_channel=AsyncMock(return_value=channel))
    settings = Mock(return_value=ServerSettings(123, 456, True))
    users = Mock(return_value=[
        UserReminderPreferences(1, frozenset({"C9", "KC"}), all_games_server_ping=True),
        UserReminderPreferences(2, frozenset({"C9"})),
        UserReminderPreferences(3, frozenset({"C9"}), followed_team_server_ping=False, dm_enabled=True),
    ])
    delivery = ServerReminderDelivery(client, store, 123, clock=lambda: NOW, load_settings=settings, load_users=users)
    return delivery, store, channel, settings, users


def test_public_message_has_only_team_names_bold_and_explicit_deduplicated_mentions():
    delivery, store, channel, _, _ = make_delivery()
    match_id = store.data.matches[0].id
    assert asyncio.run(delivery.deliver(match_id))
    call = channel.send.call_args
    text = rendered_message(call).description
    assert not call.args and call.kwargs["content"] is None
    assert "### __Subscribers:__" in text
    match = store.data.matches[0]
    stage = store.data.stages_by_id[match.stage_id]
    assert f"*Starts in 5 minutes!*\n-# *({stage.name} · Bo{match.best_of})*" in text
    assert "### __Subscribers:__\n\n<@1> <@2>\n\n-#" in text
    assert call.kwargs["allowed_mentions"].to_dict() == {"users": [1, 2], "parse": []}
    assert not asyncio.run(delivery.deliver(match_id))
    channel.send.assert_awaited_once()


def test_results_card_preserves_only_opted_in_pings_and_groups_voters_by_team():
    delivery, store, channel, _, _ = make_delivery()
    delivery.poll_manager = SimpleNamespace(prepare_reminder=AsyncMock(return_value="Voting Results\n**C9**: 100% (1 vote)\n@lua\n**KC**: 0% (0 votes)\n\u200b"))
    assert asyncio.run(delivery.deliver(store.data.matches[0].id))
    call = channel.send.call_args
    text = rendered_message(call).description
    assert "*Starts in 5 minutes!*" in text
    assert "### __Voting Results__" in text
    assert "### **C9**\n100%\n(1 vote)\n\n@lua" in text
    assert "### **KC**\n0%\n(0 votes)" in text
    assert text.index("@lua") < text.index("### **KC**") < text.index("<@1>")
    assert call.kwargs["allowed_mentions"].to_dict() == {"users": [1, 2], "parse": []}


@pytest.mark.parametrize("ping_role", [True, False])
@pytest.mark.parametrize("with_people", [True, False])
def test_worlds_role_appears_before_individual_subscribers(ping_role, with_people):
    delivery, store, channel, settings, users = make_delivery()
    settings.return_value = replace(settings.return_value, worlds_role_id=444)
    delivery.ping_role = ping_role
    if not with_people:
        users.return_value = []
    assert asyncio.run(delivery.deliver(store.data.matches[0].id))
    call = channel.send.call_args
    text = rendered_message(call).description
    assert "### __Subscribers:__\n<@&444>" in text
    if with_people:
        assert "<@&444>\n\n<@1> <@2>" in text
    else:
        assert "<@1>" not in text
    allowed = call.kwargs["allowed_mentions"].to_dict()
    assert allowed.get("roles", []) == ([444] if ping_role else [])
    assert "everyone" not in allowed["parse"]
    assert allowed.get("users", []) == ([1, 2] if with_people else [])


def test_results_test_label_and_footnotes_are_inside_card():
    delivery, store, channel, _, users = make_delivery()
    users.return_value = []
    delivery.message_prefix = "🧪 Prediction test: simulated result, no pings"
    delivery.poll_manager = SimpleNamespace(prepare_reminder=AsyncMock(return_value="Voting Results\n**C9**: 0% (0 votes)\n\u200b\n**KC**: 0% (0 votes)\n\u200b"))
    assert asyncio.run(delivery.deliver(store.data.matches[0].id))
    call = channel.send.call_args
    text = rendered_message(call).description
    assert delivery.message_prefix in text
    assert "-# • *Use `/currentvotes`" in text
    assert text.endswith("prediction standings.*")


def test_worlds_role_members_are_not_also_individually_pinged():
    delivery, store, channel, settings, _ = make_delivery()
    settings.return_value = replace(settings.return_value, worlds_role_id=444)
    channel.guild.get_member = lambda user_id: SimpleNamespace(roles=[SimpleNamespace(id=444)] if user_id == 1 else [])
    assert asyncio.run(delivery.deliver(store.data.matches[0].id))
    call = channel.send.call_args
    text = rendered_message(call).description
    assert "<@&444>\n\n<@2>" in text
    assert "<@1>" not in text
    assert call.kwargs["allowed_mentions"].to_dict() == {"roles": [444], "users": [2], "parse": []}


def test_missing_embed_permission_preserves_reminder_and_results():
    delivery, store, channel, _, _ = make_delivery()
    delivery.poll_manager = SimpleNamespace(prepare_reminder=AsyncMock(return_value="Prediction results\n**C9**: 100% (1 vote)\nLua\n**KC**: 0% (0 votes)\nNo predictions."))
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, embed_links=False)
    assert asyncio.run(delivery.deliver(store.data.matches[0].id))
    assert "Voting Results" in rendered_message(channel.send.call_args).description
    assert "embed" not in channel.send.call_args.kwargs
    assert "<@1> <@2>" in rendered_message(channel.send.call_args).description


def test_public_message_is_sent_even_without_ping_recipients():
    delivery, store, channel, _, users = make_delivery()
    users.return_value = []
    assert asyncio.run(delivery.deliver(store.data.matches[0].id))
    assert channel.send.call_args.kwargs["content"] is None
    assert "Starts in 5 minutes!" in rendered_message(channel.send.call_args).description


def test_failed_send_retries_and_only_success_consumes_key():
    delivery, store, channel, _, _ = make_delivery()
    channel.send.side_effect = [
        discord.HTTPException(SimpleNamespace(status=500, reason="Unavailable"), "temporary"), SimpleNamespace(id=9876),
    ]
    match_id = store.data.matches[0].id
    key = ReminderKey(match_id, ReminderType.SERVER)
    assert not asyncio.run(delivery.deliver(match_id))
    assert not delivery.tracker.has_sent(key)
    assert get_delivery_record(123, match_id) is None
    delivery.clock = lambda: NOW + timedelta(seconds=15)
    assert asyncio.run(delivery.deliver(match_id))
    assert delivery.tracker.has_sent(key)
    assert channel.send.await_count == 2
    assert get_delivery_record(123, match_id).message_id == 9876


@pytest.mark.parametrize("enabled, channel_id", [(False, 456), (True, None)])
def test_unconfigured_or_disabled_delivery_sends_nothing(enabled, channel_id):
    delivery, store, channel, settings, _ = make_delivery()
    settings.return_value = ServerSettings(123, channel_id, enabled)
    assert not asyncio.run(delivery.deliver(store.data.matches[0].id))
    channel.send.assert_not_awaited()


@pytest.mark.parametrize("mutation", [
    lambda s: setattr(s, "last_error", "offline"),
    lambda s: setattr(s, "fetched_at", NOW - timedelta(minutes=6)),
    lambda s: setattr(s, "data", replace(s.data, matches=[])),
    lambda s: setattr(s, "data", replace(s.data, matches=[replace(s.data.matches[0], status=MatchStatus.CANCELLED)])),
    lambda s: setattr(s, "data", replace(s.data, matches=[replace(s.data.matches[0], team1_id="TBD")])),
    lambda s: setattr(s, "data", replace(s.data, matches=[replace(s.data.matches[0], start_time=NOW + timedelta(hours=1))])),
])
def test_ineligible_or_stale_matches_never_send(mutation):
    delivery, store, channel, _, _ = make_delivery()
    match_id = store.data.matches[0].id
    mutation(store)
    assert not asyncio.run(delivery.deliver(match_id))
    channel.send.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["guild", "permissions", "type"])
def test_invalid_channel_or_missing_permissions_never_send(invalid):
    delivery, store, channel, _, _ = make_delivery()
    if invalid == "guild":
        channel.guild.id = 999
    elif invalid == "permissions":
        channel.permissions_for.return_value.send_messages = False
    else:
        channel.type = discord.ChannelType.news
    assert not asyncio.run(delivery.deliver(store.data.matches[0].id))
    channel.send.assert_not_awaited()


def test_channel_fetch_rechecks_schedule_before_sending():
    delivery, store, channel, _, _ = make_delivery()
    match_id = store.data.matches[0].id
    delivery.client.get_channel.return_value = None
    async def fetch(channel_id):
        store.data = replace(store.data, matches=[replace(store.data.matches[0], status=MatchStatus.LIVE)])
        return channel
    delivery.client.fetch_channel.side_effect = fetch
    assert not asyncio.run(delivery.deliver(match_id))
    channel.send.assert_not_awaited()


def test_setting_changes_during_io_cancel_send():
    delivery, store, channel, settings, _ = make_delivery()
    settings.side_effect = [ServerSettings(123, 456, True), ServerSettings(123, 456, False)]
    assert not asyncio.run(delivery.deliver(store.data.matches[0].id))
    channel.send.assert_not_awaited()


def test_concurrent_checks_send_one_public_message():
    delivery, store, channel, _, _ = make_delivery()
    async def scenario():
        return await asyncio.gather(*[delivery.deliver(store.data.matches[0].id) for _ in range(2)])
    assert asyncio.run(scenario()) == [True, False]
    channel.send.assert_awaited_once()


def test_server_settings_survive_reinitialization_and_keep_guilds_separate(database):
    assert get_server_settings(123) == ServerSettings(123)
    set_reminder_channel(123, 456)
    assert get_server_settings(123) == ServerSettings(123, 456, True)
    set_public_reminders_enabled(123, False)
    set_reminder_channel(123, 789)
    initialize_server_settings()
    assert get_server_settings(123) == ServerSettings(123, 789, False)
    assert get_server_settings(999) == ServerSettings(999)
    with pytest.raises(ValueError):
        set_public_reminders_enabled(999, True)


def test_server_setup_is_private_and_rechecks_admin_permission(database):
    async def scenario():
        view = ServerSetupView(123)
        assert view.toggle_reminders.disabled
        assert "Off" in build_server_setup_content(123)
        interaction = SimpleNamespace(
            guild_id=123, user=SimpleNamespace(id=7, guild_permissions=SimpleNamespace(manage_guild=False)),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        assert not await view.interaction_check(interaction)
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
        interaction.user.guild_permissions.manage_guild = True
        assert await view.interaction_check(interaction)
        interaction.user.guild_permissions.manage_guild = False
        interaction.user.roles = [SimpleNamespace(name="Admin")]
        assert not await view.interaction_check(interaction)
        view.stop()
    asyncio.run(scenario())


def test_server_setup_channel_selection_and_toggle_save_settings(database):
    async def scenario():
        _, _, channel, _, _ = make_delivery()
        view = ServerSetupView(123)
        selector = next(item for item in view.children if isinstance(item, discord.ui.ChannelSelect))
        selector._values = [SimpleNamespace(id=456)]
        interaction = SimpleNamespace(
            client=SimpleNamespace(get_channel=Mock(return_value=channel)),
            response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
        )
        await selector.callback(interaction)
        assert get_server_settings(123) == ServerSettings(123, 456, True)
        panel = interaction.edit_original_response.call_args.kwargs
        assert panel["content"] is None
        assert "<#456>" in rendered_payload(panel).description
        assert panel["view"].controls.toggle_reminders.disabled is False
        await panel["view"].controls.toggle_reminders.callback(interaction)
        assert get_server_settings(123).public_reminders_enabled is False
        assert "5 minutes before" not in build_server_setup_content(123)
        panel["view"].stop()
        interaction.response.edit_message.call_args.kwargs["view"].stop()
        view.stop()
    asyncio.run(scenario())


def test_server_setup_rejects_an_inaccessible_channel(database):
    async def scenario():
        _, _, channel, _, _ = make_delivery()
        channel.permissions_for.return_value.send_messages = False
        view = ServerSetupView(123)
        selector = next(item for item in view.children if isinstance(item, discord.ui.ChannelSelect))
        selector._values = [SimpleNamespace(id=456)]
        interaction = SimpleNamespace(
            client=SimpleNamespace(get_channel=Mock(return_value=channel)),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
        )
        await selector.callback(interaction)
        assert get_server_settings(123) == ServerSettings(123)
        interaction.edit_original_response.assert_not_awaited()
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        view.stop()
    asyncio.run(scenario())


def test_new_sender_after_restart_does_not_repeat_successful_delivery():
    delivery, store, channel, settings, users = make_delivery()
    match_id = store.data.matches[0].id
    assert asyncio.run(delivery.deliver(match_id))
    assert get_delivery_record(123, match_id) == DeliveryRecord(123, match_id, 456, 9876, NOW)
    initialize_delivery_records()  # Same startup migration on an existing database.
    restarted = ServerReminderDelivery(
        delivery.client, store, 123, clock=lambda: NOW, load_settings=settings, load_users=users,
    )
    assert not restarted.tracker.has_sent(ReminderKey(match_id, ReminderType.SERVER))
    assert not asyncio.run(restarted.deliver(match_id))
    channel.send.assert_awaited_once()


def test_leaderboard_preview_is_private_populated_and_does_not_write_results():
    with user_settings.get_connection() as connection:
        before = list(connection.iterdump())
    async def scenario():
        view = ServerSetupView(123)
        assert not view.preview_leaderboard.disabled
        interaction = SimpleNamespace(
            guild_id=123, user=SimpleNamespace(id=7, display_name="lua", guild_permissions=discord.Permissions(manage_guild=True)),
            client=SimpleNamespace(voting_manager=SimpleNamespace(store=SimpleNamespace(data=SimpleNamespace(tournament=SimpleNamespace(id="worlds-2026"))))),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        assert await view.interaction_check(interaction)
        await view.preview_leaderboard.callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        messages = interaction.followup.send.call_args_list
        text = "\n".join(rendered_message(call).description for call in messages)
        assert "Sample Player 6" in text and "Sample Player 7" not in text and "18 correct" in text
        assert "real leaderboard is unchanged" in text
        for call in messages:
            assert call.kwargs["ephemeral"] is True
            assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        view.stop()
    asyncio.run(scenario())
    with user_settings.get_connection() as connection:
        assert list(connection.iterdump()) == before


def test_channel_change_or_reschedule_does_not_repeat_delivered_match():
    delivery, store, channel, settings, users = make_delivery()
    original = store.data.matches[0]
    assert asyncio.run(delivery.deliver(original.id))
    settings.return_value = ServerSettings(123, 789, True)
    later = NOW + timedelta(hours=1)
    store.fetched_at = later
    store.data = replace(store.data, matches=[replace(original, start_time=later + timedelta(minutes=5))])
    restarted = ServerReminderDelivery(
        delivery.client, store, 123, clock=lambda: later, load_settings=settings, load_users=users,
    )
    assert not asyncio.run(restarted.deliver(original.id))
    channel.send.assert_awaited_once()


def test_failure_before_success_is_retryable_after_restart():
    delivery, store, channel, settings, users = make_delivery()
    match_id = store.data.matches[0].id
    channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="Unavailable"), "temporary")
    assert not asyncio.run(delivery.deliver(match_id))
    assert get_delivery_record(123, match_id) is None
    channel.send.side_effect = None
    restarted = ServerReminderDelivery(
        delivery.client, store, 123, clock=lambda: NOW, load_settings=settings, load_users=users,
    )
    assert asyncio.run(restarted.deliver(match_id))
    assert get_delivery_record(123, match_id).message_id == 9876


def test_database_read_failure_prevents_discord_send(monkeypatch):
    delivery, store, channel, _, _ = make_delivery()
    def fail_read(*args):
        raise sqlite3.OperationalError("database unavailable")
    monkeypatch.setattr("services.server_reminders.get_delivery_record", fail_read)
    assert not asyncio.run(delivery.deliver(store.data.matches[0].id))
    channel.send.assert_not_awaited()


def test_failed_receipt_write_is_retried_without_resending(monkeypatch):
    delivery, store, channel, _, _ = make_delivery()
    calls = 0
    def save(record):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database locked")
        record_delivery(record)
    monkeypatch.setattr("services.server_reminders.record_delivery", save)
    match_id = store.data.matches[0].id
    assert asyncio.run(delivery.deliver(match_id))
    assert get_delivery_record(123, match_id) is None
    assert not asyncio.run(delivery.deliver(match_id))
    asyncio.run(delivery.flush_pending_records())
    assert get_delivery_record(123, match_id).message_id == 9876
    assert not delivery._pending_records
    channel.send.assert_awaited_once()


def test_receipt_uniqueness_preserves_first_send_and_separates_servers():
    first = DeliveryRecord(123, "match", 456, 9876, NOW)
    record_delivery(first)
    record_delivery(replace(first, channel_id=789, message_id=9877))
    other_server = replace(first, guild_id=999)
    record_delivery(other_server)
    assert get_delivery_record(123, "match") == first
    assert get_delivery_record(999, "match") == other_server


def test_receipts_normalize_utc_and_reject_naive_time():
    from datetime import timezone
    timestamp = NOW.astimezone(timezone(timedelta(hours=2)))
    record_delivery(DeliveryRecord(123, "match", 456, 9876, timestamp))
    assert get_delivery_record(123, "match").sent_at.isoformat() == NOW.isoformat()
    with pytest.raises(ValueError):
        record_delivery(DeliveryRecord(123, "other", 456, 9877, NOW.replace(tzinfo=None)))


def test_shutdown_cancellation_before_discord_ack_does_not_record_success():
    delivery, store, channel, _, _ = make_delivery()
    match_id = store.data.matches[0].id
    async def scenario():
        entered, blocked = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await blocked.wait()
        channel.send.side_effect = send
        task = asyncio.create_task(delivery.deliver(match_id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert get_delivery_record(123, match_id) is None
    assert not delivery.tracker.has_sent(ReminderKey(match_id, ReminderType.SERVER))


@pytest.mark.parametrize("elapsed, expected", [
    (120, "3 minutes"), (240, "1 minute"), (270, "less than a minute"),
])
def test_restart_catches_up_before_start_with_correct_wording(elapsed, expected):
    delivery, store, channel, _, _ = make_delivery()
    delivery.clock = lambda: NOW + timedelta(seconds=elapsed)
    assert asyncio.run(delivery.deliver(store.data.matches[0].id))
    assert f"Starts in {expected}!" in rendered_message(channel.send.call_args).description
    assert get_delivery_record(123, store.data.matches[0].id) is not None


@pytest.mark.parametrize("elapsed", [-1, 300, 301, 900])
def test_recovery_never_sends_early_or_after_start(elapsed):
    delivery, store, channel, _, _ = make_delivery()
    delivery.clock = lambda: NOW + timedelta(seconds=elapsed)
    store.fetched_at = delivery.clock()
    assert not asyncio.run(delivery.deliver(store.data.matches[0].id))
    channel.send.assert_not_awaited()


def test_transient_send_failures_back_off_and_expire_at_match_start():
    delivery, store, channel, _, _ = make_delivery()
    match_id = store.data.matches[0].id
    current = NOW
    delivery.clock = lambda: current
    channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "temporary")
    for delay in [15, 30, 60, 60]:
        assert not asyncio.run(delivery.deliver(match_id))
        assert delivery._retry_after[match_id] == current + timedelta(seconds=delay)
        before = channel.send.await_count
        assert not asyncio.run(delivery.deliver(match_id))
        assert channel.send.await_count == before
        current += timedelta(seconds=delay)
    current = NOW + timedelta(minutes=5)
    before = channel.send.await_count
    assert not asyncio.run(delivery.deliver(match_id))
    assert channel.send.await_count == before
    assert get_delivery_record(123, match_id) is None


def test_discord_rate_limit_is_respected():
    delivery, store, channel, _, _ = make_delivery()
    channel.send.side_effect = discord.RateLimited(90)
    match_id = store.data.matches[0].id
    assert not asyncio.run(delivery.deliver(match_id))
    delivery.clock = lambda: NOW + timedelta(seconds=60)
    assert not asyncio.run(delivery.deliver(match_id))
    channel.send.assert_awaited_once()
    channel.send.side_effect = None
    delivery.clock = lambda: NOW + timedelta(seconds=90)
    assert asyncio.run(delivery.deliver(match_id))


def test_deleted_channel_failure_is_actionable_and_throttled(caplog):
    delivery, store, channel, _, _ = make_delivery()
    channel.send.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "Unknown channel")
    match_id = store.data.matches[0].id
    assert not asyncio.run(delivery.deliver(match_id))
    assert "/serversetup" in caplog.text
    assert delivery._retry_after[match_id] == NOW + timedelta(seconds=60)
    assert not asyncio.run(delivery.deliver(match_id))
    channel.send.assert_awaited_once()


def test_controlled_delivery_test_posts_once_without_mentions_and_leaves_real_matches_untouched():
    delivery, store, channel, _, _ = make_delivery()
    original = list(store.data.matches)
    set_reminder_channel(123, 456)
    message = asyncio.run(run_reminder_test(delivery.client, store, 123))
    assert "Test passed" in message
    assert "second attempt" in message
    channel.send.assert_awaited_once()
    call = channel.send.call_args
    assert "Delivery test" in rendered_message(call).description
    assert "<@" not in rendered_message(call).description
    assert call.kwargs["allowed_mentions"].to_dict() == {"users": [], "parse": []}
    assert store.data.matches == original
    assert get_delivery_record(123, original[0].id) is None
    with user_settings.get_connection() as connection:
        rows = connection.execute("SELECT match_id FROM server_reminder_deliveries").fetchall()
    assert len(rows) == 1
    assert rows[0][0].startswith("test-reminder:")


def test_controlled_test_respects_disabled_public_reminders():
    delivery, store, channel, _, _ = make_delivery()
    assert "wasn't sent" in asyncio.run(run_reminder_test(delivery.client, store, 123))
    channel.send.assert_not_awaited()


def test_five_minute_test_uses_saved_personal_settings_and_never_pings_roles():
    delivery, store, channel, _, _ = make_delivery()
    first = store.data.matches[0]
    set_reminder_channel(123, 456)
    user_settings.set_followed_team_ids(1, {first.team1_id})
    user_settings.set_followed_team_ids(2, {first.team2_id})
    user_settings.set_server_ping(2, False)
    user_settings.set_followed_team_ids(3, {"T1"})
    user_settings.set_all_games_server_ping(4, True)
    result = asyncio.run(run_reminder_test(delivery.client, store, 123, ping_users=True))
    assert "Test passed" in result and "`/mysetup`" in result
    channel.send.assert_awaited_once()
    call = channel.send.call_args
    text = rendered_message(call).description
    assert "Starts in 5 minutes!" in text
    assert "<@1>" in text and "<@4>" not in text
    assert "<@2>" not in text and "<@3>" not in text
    assert call.kwargs["allowed_mentions"].to_dict() == {"users": [1], "parse": []}
    assert get_delivery_record(123, first.id) is None


def test_five_minute_test_button_enables_real_preference_targeting(monkeypatch):
    callback = AsyncMock(return_value="Test passed")
    monkeypatch.setattr("services.server_setup.run_reminder_test", callback)
    set_reminder_channel(123, 456)
    async def scenario():
        view = ServerSetupView(123)
        interaction = SimpleNamespace(
            client=SimpleNamespace(background_polling=SimpleNamespace(store=object())),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        assert not view.test_reminder_with_pings.disabled
        await view.test_reminder_with_pings.callback(interaction)
        callback.assert_awaited_once_with(interaction.client, interaction.client.background_polling.store, 123, ping_users=True)
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        view.stop()
    asyncio.run(scenario())


def test_controlled_test_does_not_retry_from_new_sender_without_receipt(monkeypatch):
    delivery, store, channel, _, _ = make_delivery()
    set_reminder_channel(123, 456)
    def fail_write(*args):
        raise sqlite3.OperationalError("disk unavailable")
    monkeypatch.setattr("services.server_reminders.record_delivery", fail_write)
    result = asyncio.run(run_reminder_test(delivery.client, store, 123))
    assert "could not be saved" in result
    channel.send.assert_awaited_once()


def test_controlled_test_button_returns_private_result(database, monkeypatch):
    callback = AsyncMock(return_value="Test passed")
    monkeypatch.setattr("services.server_setup.run_reminder_test", callback)
    set_reminder_channel(123, 456)
    async def scenario():
        view = ServerSetupView(123)
        assert not view.test_reminder.disabled
        interaction = SimpleNamespace(
            client=SimpleNamespace(background_polling=SimpleNamespace(store=object())),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )
        await view.test_reminder.callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        assert rendered_message(interaction.followup.send.call_args).description == "Test passed"
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        view.stop()
    asyncio.run(scenario())


def test_pickems_link_persists_without_changing_channel_or_toggle():
    set_reminder_channel(123, 456)
    set_public_reminders_enabled(123, False)
    link = "https://pickem.lolesports.com/example-group"
    set_pickems_url(123, link)
    initialize_server_settings()
    assert get_server_settings(123) == ServerSettings(123, 456, False, link)
    for bad in ("javascript:alert(1)", "http://example.com", "https://user:password@example.com"):
        with pytest.raises(ValueError):
            set_pickems_url(123, bad)
    assert get_server_settings(123).pickems_url == link


def test_pickems_modal_saves_link_privately_after_admin_check():
    async def scenario():
        modal = PickemsLinkModal(123)
        modal.link._value = "https://pickem.lolesports.com/example-group"
        interaction = SimpleNamespace(
            guild_id=123, user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False)),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await modal.on_submit(interaction)
        assert get_server_settings(123).pickems_url is None
        interaction.user.guild_permissions.manage_guild = True
        await modal.on_submit(interaction)
        assert get_server_settings(123).pickems_url == modal.link.value
        assert interaction.response.send_message.call_args.kwargs["ephemeral"]
    asyncio.run(scenario())
