"""An admin-triggered, no-ping test day isolated from the Worlds leaderboard."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import logging
from types import SimpleNamespace
from uuid import uuid4

import discord
from services.presentation import embed_card_payload, message_payload
from services.prediction_display import my_votes_embeds
from services.user_settings import get_user_timezone, get_user_time_format

from models import MatchStatus
from services import polls
from services.prediction_stats import get_competition_stats
from services.server_reminders import ServerReminderDelivery
from services.voting import VotingManager, MATCHDAY_ZONE


logger = logging.getLogger(__name__)


async def start_prediction_test(interaction, guild_id):
    background = interaction.client.background_polling
    if background.prediction_test_running:
        return "A prediction test is already running. Wait for its result before starting another."
    source = background.store.data
    templates = [m for m in source.matches if "TBD" not in {m.team1_id, m.team2_id}][:2]
    if len(templates) < 2:
        return "The test needs two known matchups in the tournament data."
    token = f"prediction-test:{uuid4()}"
    now = datetime.now(UTC)
    matches = [replace(
        match, id=f"{token}:{index}", tournament_id=token, start_time=now + timedelta(minutes=6 + index),
        status=MatchStatus.SCHEDULED, winner_id=None, team1_score=None, team2_score=None,
    ) for index, match in enumerate(templates)]
    data = replace(source, tournament=replace(source.tournament, id=token), matches=matches, standings=[])
    store = SimpleNamespace(data=data, fetched_at=now, last_error=None)
    manager = VotingManager(interaction.client, store, guild_id, clock=lambda: datetime.now(UTC), test_mode=True)
    sender = ServerReminderDelivery(
        interaction.client, store, guild_id, load_users=lambda: [], poll_manager=manager,
        clock=lambda: datetime.now(UTC),
        message_prefix="🧪 Prediction test: simulated result, no pings",
    )
    background.prediction_test_running = True
    interaction.client.prediction_test_manager = manager
    try:
        await manager.tick()
        day = matches[0].start_time.astimezone(MATCHDAY_ZONE).date().isoformat()
        if polls.get_daily_post(guild_id, token, day) is None:
            background.prediction_test_running = False
            interaction.client.prediction_test_manager = None
            return "The test daily post wasn't sent. Check that public reminders are On and review the bot logs."
    except Exception:
        background.prediction_test_running = False
        interaction.client.prediction_test_manager = None
        raise

    async def run():
        done = set()
        try:
            deadline = now + timedelta(minutes=3)
            while datetime.now(UTC) < deadline and len(done) < len(matches):
                store.fetched_at = datetime.now(UTC)
                await manager.tick()
                for match in matches:
                    if match.id not in done and datetime.now(UTC) >= match.start_time - timedelta(minutes=5):
                        if await sender.deliver(match.id):
                            done.add(match.id)
                            store.data = replace(store.data, matches=[
                                replace(m, status=MatchStatus.COMPLETED, winner_id=m.team1_id,
                                        team1_score=m.best_of // 2 + 1, team2_score=0)
                                if m.id == match.id else m for m in store.data.matches
                            ])
                await manager.tick()
                if len(done) < len(matches):
                    await asyncio.sleep(5)
            rows = get_competition_stats(guild_id, token)
            with_names = {}
            for match in matches:
                for user_id, _, name in polls.get_poll_results(guild_id, match.id):
                    with_names[user_id] = name or f"User {user_id}"
            text = "Test completed. Simulated winners were the first team in each matchup.\n"
            text += "\n".join(
                f"{discord.utils.escape_mentions(discord.utils.escape_markdown(with_names.get(row.user_id, str(row.user_id))))}: "
                f"{row.correct}/{row.submitted} correct · {float(row.accuracy):.1%} accuracy · "
                f"{row.submitted}/{row.eligible} predicted · {float(row.participation):.1%} participation\n"
                f"-# Current streak: {row.current_streak} · Best streak: {row.longest_streak}"
                for row in rows
            ) or "No test predictions were submitted."
            if len(done) != len(matches):
                text = "Prediction test did not complete all reminder sends. Check the bot logs."
            await interaction.followup.send(**message_payload(text + "\nThe real Worlds leaderboard was not changed."),
                                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            history = await asyncio.to_thread(polls.get_user_votes, guild_id, token, interaction.user.id)
            if history:
                timezone_name = await asyncio.to_thread(get_user_timezone, interaction.user.id)
                time_format = await asyncio.to_thread(get_user_time_format, interaction.user.id)
                for embed in my_votes_embeds(history, store.data, display_name=interaction.user.display_name,
                                            timezone_name=timezone_name, time_format=time_format):
                    embed.title = "__TEST: " + embed.title.removeprefix("__")
                    await interaction.followup.send(**embed_card_payload(embed), ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("Controlled prediction test failed")
            await interaction.followup.send(**message_payload("Prediction test failed. Check the bot logs."), ephemeral=True)
        finally:
            background.prediction_test_running = False
            interaction.client.prediction_test_manager = None
            await sender.flush_pending_records()
            await manager.flush_pending_records()
    background.manage_test_task(asyncio.create_task(run(), name="controlled-prediction-test"))
    return (
        "Test daily post sent. Vote using its buttons: the first match closes in about one minute, "
        "the second in about two. No users will be pinged. Keep the bot running; a private test scoreboard will follow."
    )
