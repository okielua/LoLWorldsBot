"""An explicit admin-triggered delivery test using an isolated synthetic match."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from models import MatchStatus
from services.delivery_records import get_delivery_record
from services.server_reminders import ServerReminderDelivery
from services.reminder_engine import ReminderKey, ReminderType


async def run_reminder_test(client, store, guild_id):
    template = next((match for match in store.data.matches
                     if "TBD" not in {match.team1_id, match.team2_id}), None)
    if template is None:
        return "No known team matchup is available for the test. Check the live-data refresh logs."
    now = datetime.now(UTC)
    match = replace(
        template, id=f"test-reminder:{uuid4()}", start_time=now + timedelta(minutes=5),
        status=MatchStatus.SCHEDULED, team1_score=None, team2_score=None, winner_id=None,
    )
    test_store = SimpleNamespace(
        data=replace(store.data, matches=[match]), fetched_at=now, last_error=None,
    )
    def sender():
        return ServerReminderDelivery(
            client, test_store, guild_id, clock=lambda: now,
            load_users=lambda: [],
            message_prefix="🧪 Delivery test: simulated match, no pings",
        )
    delivery = sender()
    if not await delivery.deliver(match.id):
        return "Test wasn't sent. Check that public reminders are On, the channel permissions are correct, and the bot logs show no errors."
    await delivery.flush_pending_records()
    receipt = get_delivery_record(guild_id, match.id)
    if receipt is None:
        return "The test message was sent, but its SQLite receipt could not be saved. Check the bot logs before proceeding."
    # A new sender has no in-memory keys: this exercises the persistent restart check.
    restarted = sender()
    if await restarted.deliver(match.id):
        return "Duplicate suppression failed. Check the bot logs before proceeding."
    if not restarted.tracker.has_sent(ReminderKey(match.id, ReminderType.SERVER)):
        return "The test message was sent, but the persistent duplicate check could not be verified. Check the bot logs."
    link = f"https://discord.com/channels/{guild_id}/{receipt.channel_id}/{receipt.message_id}"
    return (
        f"Test passed: [view the public test message]({link}).\n"
        "SQLite receipt saved; a second attempt from a fresh sender was suppressed.\n"
        "No users were pinged."
    )
