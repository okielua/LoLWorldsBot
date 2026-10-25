import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from presentation_assertions import rendered_payload
from models import MatchStatus

from services import user_settings
from services.leaderboard_display import sample_leaderboard_entries
from services.leaderboard_views import MyStatsButton, leaderboard_payload, show_personal_stats
from services.polls import create_poll, cast_vote, initialize_polls, record_poll_opportunity, reconcile_polls
from services.tournament_data import load_tournament_data


@pytest.fixture
def stats_data(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "stats.db")
    user_settings.initialize_database()
    initialize_polls()
    for user_id in (7, 8):
        user_settings.mark_setup_completed(123, user_id)
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json")
    start = datetime(2026, 10, 16, 18, tzinfo=UTC)
    swiss = next(stage for stage in data.stages if "swiss" in stage.name.lower())
    matches = [replace(match, start_time=start + timedelta(hours=i), stage_id=swiss.id) for i, match in enumerate(data.matches[:2])]
    for i, match in enumerate(matches):
        create_poll(123, match)
        record_poll_opportunity(123, match.id, match.team1_id, match.team2_id, start - timedelta(hours=1))
        cast_vote(123, match.id, 7, match.team1_id, start - timedelta(minutes=20), "lua")
        cast_vote(123, match.id, 8, match.team2_id, start - timedelta(minutes=20), "Other user")
    completed = replace(matches[0], status=MatchStatus.COMPLETED, winner_id=matches[0].team1_id)
    data = replace(data, matches=[completed, matches[1]])
    reconcile_polls(123, data, start + timedelta(minutes=10))
    return data


def interaction_for(data, user_id):
    members = {7: SimpleNamespace(id=7, display_name="lua"), 8: SimpleNamespace(id=8, display_name="Other user")}
    return SimpleNamespace(
        guild_id=123, guild=SimpleNamespace(get_member=members.get), user=members[user_id],
        client=SimpleNamespace(voting_manager=SimpleNamespace(store=SimpleNamespace(data=data))),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
    )


def test_playin_votes_count_in_leaderboard_and_personal_stats_with_result_protection(stats_data, monkeypatch):
    from services import spoilers
    from services.prediction_stats import get_competition_snapshot
    from services.spoiler_delivery import render_payload

    playin = next(stage for stage in stats_data.stages if "play" in stage.name.lower())
    completed = replace(stats_data.matches[0], stage_id=playin.id)
    data = replace(stats_data, matches=[completed, stats_data.matches[1]])
    now = completed.start_time + timedelta(minutes=30)
    policy = spoilers.SpoilerPolicy(data, ended_at={completed.id: now}, clock=lambda: now)
    monkeypatch.setattr(spoilers, "_current", policy)
    snapshot = get_competition_snapshot(123, data.tournament.id, data=data)
    assert snapshot.result_match_ids == (completed.id,) and snapshot.eligible == 1
    assert policy.protected(completed)

    async def scenario():
        interaction = interaction_for(data, 7)
        from services.leaderboard_views import leaderboard_context
        _, entries, _ = await leaderboard_context(interaction, data.tournament.id, data=data)
        assert {row.user_id: (row.correct, row.submitted, row.eligible) for row, _ in entries} == {
            7: (1, 1, 1), 8: (0, 1, 1)}
        await show_personal_stats(interaction, 123, data.tournament.id, data=data, target_user=interaction.user)
        reply = interaction.followup.send.call_args.kwargs
        assert reply["ephemeral"] is True
        rendered = render_payload({"components": reply["view"].to_components()})
        text = "\n".join(child["content"] for card in rendered["components"] for child in card["components"]
                         if child.get("type") == 10)
        assert "### · Correct predictions: ||1/1|| (||100%||)" in text
        assert "__*Check Your PayPal*__" in text
        assert text.count("Not enough data") == 4
        assert 'including Play-Ins' in text
    asyncio.run(scenario())


def test_button_and_stats_command_return_identical_private_clicker_stats(stats_data):
    from test_bot_commands import load_command
    data = stats_data
    async def scenario():
        caller = interaction_for(data, 8)
        button = MyStatsButton(123, data.tournament.id)
        await button.callback(caller)
        clicked = caller.followup.send.call_args.kwargs
        rendered = rendered_payload(clicked)
        assert rendered.title == "__Worlds 2026 Voting Stats · `Other user`__"
        assert "### · Correct predictions: 0/1 (0%)" in rendered.description
        assert "### · Correct predictions: 0/1 (0%)" in rendered.description
        assert "Pending votes" not in rendered.description
        assert all(label in rendered.description for label in ("## :PeyzPal: __*Check Your PayPal*__", "## :PepeKMS: __*Can’t Read These Frauds*__", "## :Copium: __*Hopium Supplier*__", "## :Discheesed: __*Democracy Was a Mistake*__"))
        first_text = next(item.content for item in clicked["view"].walk_children() if isinstance(item, discord.ui.TextDisplay))
        assert first_text.startswith("# __Worlds 2026 Voting Stats · `Other user`__\n")
        caller.response.defer.assert_awaited_once_with(ephemeral=True)
        assert clicked["ephemeral"] is True
        assert clicked["allowed_mentions"].to_dict() == {"parse": []}
        command_caller = interaction_for(data, 8)
        await load_command("stats", data, discord=discord)(command_caller, command_caller.user)
        assert rendered_payload(command_caller.followup.send.call_args.kwargs) == rendered
        owner = interaction_for(data, 7)
        await button.callback(owner)
        assert "### · Correct predictions: 1/1 (100%)" in rendered_payload(owner.followup.send.call_args.kwargs).description
    asyncio.run(scenario())


def test_stats_button_restores_scope_after_restart_and_rejects_other_guilds(stats_data):
    async def scenario():
        original = MyStatsButton(123, stats_data.tournament.id)
        caller = interaction_for(stats_data, 8)
        restored = await MyStatsButton.from_custom_id(caller, original.item, MyStatsButton.__discord_ui_compiled_template__.fullmatch(original.custom_id))
        assert restored.guild_id == 123 and restored.tournament_id == stats_data.tournament.id
        await restored.callback(caller)
        assert "Other user" in rendered_payload(caller.followup.send.call_args.kwargs).title
        other = interaction_for(stats_data, 8)
        other.guild_id = 999
        await restored.callback(other)
        other.response.send_message.assert_awaited_once()
        assert other.response.send_message.call_args.kwargs["ephemeral"] is True
        other.followup.send.assert_not_awaited()
    asyncio.run(scenario())


def test_normal_weight_card_keeps_my_stats_button_and_pagination(stats_data):
    entries = sample_leaderboard_entries("lua")
    async def scenario():
        payload = leaderboard_payload(entries, 123, stats_data.tournament.id)
        assert "embed" not in payload and payload["embeds"] == []
        assert isinstance(payload["view"], discord.ui.LayoutView)
        assert len(payload["view"].children) == 1
        button, = [item for item in payload["view"].walk_children() if isinstance(item, MyStatsButton)]
        assert button.item.label == "My Advanced Stats"
        assert button.item.style == discord.ButtonStyle.secondary
        assert str(button.item.emoji) == "🔍"
        many = [(replace(row, user_id=i * 6 + row.user_id), f"Long sample player {i} " + "x" * 90)
                for i in range(30) for row, _ in entries]
        large = leaderboard_payload(many, 123, stats_data.tournament.id)
        view = large["view"].controls
        assert len(view.pages) > 1 and view.previous.disabled
        assert all(len(embed.description) <= 4096 and len(embed) <= 6000 for embed in view.pages)
        click = interaction_for(stats_data, 8)
        await view.next_page.callback(click)
        assert view.page == 1
        click.response.defer.assert_awaited_once()
        assert click.edit_original_response.call_args.kwargs["attachments"]
        assert "files" not in click.edit_original_response.call_args.kwargs
        rendered_page = rendered_payload(click.edit_original_response.call_args.kwargs).description
        assert all(line.strip() in rendered_page for line in view.pages[1].description.splitlines()
                   if line.strip() and not line.startswith("# "))
        assert any(isinstance(item, discord.ui.MediaGallery) for item in click.edit_original_response.call_args.kwargs["view"].walk_children())
        assert click.edit_original_response.call_args.kwargs["embeds"] == []
        assert "embed" not in click.edit_original_response.call_args.kwargs
        assert len([item for item in click.edit_original_response.call_args.kwargs["view"].walk_children() if isinstance(item, MyStatsButton)]) == 1
        await view.previous.callback(click)
        assert view.page == 0 and view.previous.disabled
        view.stop()
        payload["view"].stop()
    asyncio.run(scenario())


def test_trophycase_button_includes_pending_only_voters_and_aligned_slots(stats_data):
    from services.trophycase import TrophyCaseButton
    # This player has only voted on an unresolved series and has no scored row.
    match = stats_data.matches[1]
    cast_vote(123, match.id, 9, match.team1_id, match.start_time - timedelta(hours=2), 'Pending player')
    caller = interaction_for(stats_data, 7)
    asyncio.run(TrophyCaseButton(123, stats_data.tournament.id).callback(caller))
    caller.response.defer.assert_awaited_once_with(ephemeral=True)
    payload = caller.followup.send.call_args.kwargs
    text = '\n'.join(i.content for i in payload['view'].walk_children() if isinstance(i, discord.ui.TextDisplay))
    assert 'Pending player' in text and 'Other user' in text and 'lua' in text
    assert '\N{NO-BREAK SPACE}' in text
    assert 'In total there are 12 trophies to win:' in text and '⚪' in text
    for label in ('8x Swiss matchdays', '1x Quarterfinal', '1x Semifinals', '1x Final', '1x overall Worlds 2026 voting winner'):
        assert '-# ' + '\N{NO-BREAK SPACE}' * 2 + '*· ' + label + '*' in text
    assert 'Slots run chronologically' not in text and 'Not earned' not in text and 'Earned ·' not in text
    assert payload['ephemeral'] is True and payload['allowed_mentions'].to_dict() == {'parse': []}
    payload['view'].stop()


def test_leaderboard_contains_persistent_trophycase_button():
    from services.trophycase import TrophyCaseButton
    async def scenario():
        payload = leaderboard_payload(sample_leaderboard_entries('lua'), 123, 'worlds-2026', preview=True)
        buttons = [getattr(i, 'item', i) for i in payload['view'].walk_children()]
        button = next(i for i in buttons if getattr(i, 'label', None) == 'Trophy Case')
        assert str(button.emoji) == '🏆' and button.custom_id == 'worldstrophies:123:worlds-2026'
        restored = await TrophyCaseButton.from_custom_id(None, button, {'guild':'123','tournament':'worlds-2026'})
        assert restored.guild_id == 123
        payload['view'].stop()
    asyncio.run(scenario())


def test_stats_command_can_privately_view_another_member(stats_data):
    from test_bot_commands import load_command
    async def scenario():
        caller = interaction_for(stats_data, 8)
        target = interaction_for(stats_data, 7).user
        await load_command('stats', stats_data, discord=discord)(caller, target)
        payload = caller.followup.send.call_args.kwargs
        rendered = rendered_payload(payload)
        assert rendered.title.replace('\u00a0', '') == '__Worlds 2026 Voting Stats · `lua`__'
        assert '### · Correct predictions: 1/1 (100%)' in rendered.description
        assert 'Other user’s' not in rendered.description and '### My ' not in rendered.description
        assert 'whether `lua` votes for or against them' in rendered.description.replace('\u00a0', '')
        assert payload['ephemeral'] is True and payload['allowed_mentions'].to_dict() == {'parse': []}
        payload['view'].stop()
    asyncio.run(scenario())


def test_stats_command_handles_a_member_with_no_votes(stats_data):
    from test_bot_commands import load_command
    async def scenario():
        caller = interaction_for(stats_data, 8)
        target = SimpleNamespace(id=99, display_name='New member')
        await load_command('stats', stats_data, discord=discord)(caller, target)
        payload = caller.followup.send.call_args.kwargs
        text = rendered_payload(payload).description
        assert '### · Correct predictions: 0/0 (—)' in text
        assert '### · League rank: —' in text
        assert payload['ephemeral'] is True
        payload['view'].stop()
    asyncio.run(scenario())


def test_leaderboard_uses_native_dividers_between_tiers_not_players():
    from services.prediction_stats import PredictionStats
    async def scenario():
        entries = [(PredictionStats(i, 7-i, 10, 10, 0, 0), f'Player{i}') for i in range(7)]
        payload = leaderboard_payload(entries, 123, 'worlds-2026')
        card, = payload['view'].children
        texts = [i.content for i in card.children if isinstance(i, discord.ui.TextDisplay)]
        dividers = [i for i in card.children if isinstance(i, discord.ui.Separator)]
        assert len(texts) == 6 and len(dividers) == 5
        assert texts[5].startswith("-# ") and "League ranks:" in texts[5]
        assert "-# " not in texts[4]
        assert all(i.visible and i.spacing == discord.SeparatorSpacing.large for i in dividers)
        galleries = [i for i in card.children if isinstance(i, discord.ui.MediaGallery)]
        assert len(galleries) == 4
        assert isinstance(card.children[0], discord.ui.TextDisplay) and "Leaderboard" in card.children[0].content
        assert isinstance(card.children[1], discord.ui.Separator) and card.children[1].visible
        assert isinstance(card.children[2], discord.ui.MediaGallery)
        assert all(i.items[0].description is None for i in galleries)
        assert all('description' not in i.to_component_dict()['items'][0] for i in galleries)
        assert 'Player1' in texts[2] and 'Player2' in texts[2]
        assert payload['view'].total_children_count <= 40
        payload['view'].stop()
    asyncio.run(scenario())


def test_tier_badges_render_as_images_with_dividers_controls_and_pagination(monkeypatch):
    from services import leaderboard_display, leaderboard_views
    from services.prediction_stats import PredictionStats
    from services.prediction_tiers import TIERS
    from services.presentation import embed_card_payload
    emojis = {key: discord.PartialEmoji(name=key, id=100+i) for i, (key, _, _) in enumerate(TIERS)}
    monkeypatch.setattr(leaderboard_display, 'custom_emoji', emojis.get)
    monkeypatch.setattr(leaderboard_views, 'custom_emoji', emojis.get)
    async def scenario():
        entries = [(PredictionStats(i, 7-i, 10, 10, 0, 0), f'Player{i}') for i in range(7)]
        payload = leaderboard_payload(entries, 123, 'worlds-2026')
        card, = payload['view'].children
        galleries = [i for i in card.children if isinstance(i, discord.ui.MediaGallery)]
        assert len(galleries) == 4
        assert isinstance(card.children[0], discord.ui.TextDisplay) and "Leaderboard" in card.children[0].content
        assert isinstance(card.children[1], discord.ui.Separator) and card.children[1].visible
        assert isinstance(card.children[2], discord.ui.MediaGallery)
        assert all(i.items[0].description is None for i in galleries)
        assert [i.items[0].media.url for i in galleries] == [f'attachment://worlds-rank-{key}.png' for key, _, _ in TIERS]
        assert [file.filename for file in payload['files']] == [f'worlds-rank-{key}.png' for key, _, _ in TIERS]
        import struct
        assert {struct.unpack('>II', file.fp.getvalue()[16:24]) for file in payload['files']} == {(128, 104)}
        assert all(i.items[0].media.url.startswith('attachment://') for i in galleries)
        assert all(not i.items[0].spoiler for i in galleries)
        assert len([i for i in card.children if isinstance(i, discord.ui.Separator) and i.visible]) == 5
        texts = '\n'.join(i.content for i in card.children if isinstance(i, discord.ui.TextDisplay))
        assert '# <:challenger:' not in texts and 'Player0' in texts and 'Player6' in texts
        assert payload['view'].total_children_count <= 40
        assert 'Trophy Case' in str(payload['view'].to_components())
        payload['view'].stop()
        large = [(PredictionStats(i, 40-i, 40, 40, 0, 0), f'Player{i}' + 'x'*60) for i in range(30)]
        pages = leaderboard_display.leaderboard_embeds(large, preview=True)
        assert len(pages) > 1
        for page in pages:
            controls = leaderboard_views.LeaderboardView(pages, 123, 'worlds-2026')
            view = embed_card_payload(page, controls=controls)['view']
            assert any(isinstance(i, discord.ui.MediaGallery) for i in view.walk_children())
            assert view.total_children_count <= 40
            view.stop()
    asyncio.run(scenario())


def test_badge_files_are_included_in_discord_multipart_upload():
    import json
    from discord.http import handle_message_parameters
    from services.prediction_stats import PredictionStats
    async def scenario():
        entries = [(PredictionStats(i, 7-i, 10, 10, 0, 0), f'Player{i}') for i in range(7)]
        payload = leaderboard_payload(entries, 123, 'worlds-2026')
        with handle_message_parameters(**payload) as request:
            wire = json.loads(next(part['value'] for part in request.multipart if part['name'] == 'payload_json'))
            names = [attachment['filename'] for attachment in wire['attachments']]
            assert len(names) == 4 and all(name.startswith('worlds-rank-') for name in names)
            galleries = [item for item in wire['components'][0]['components'] if item['type'] == 12]
            assert [gallery['items'][0]['media']['url'] for gallery in galleries] == ['attachment://' + name for name in names]
            assert all('description' not in gallery['items'][0] for gallery in galleries)
        payload['view'].stop()
    asyncio.run(scenario())


def test_stats_names_are_unpadded_while_leaderboard_names_stay_aligned(stats_data):
    from services.leaderboard_views import leaderboard_context
    async def scenario():
        caller = interaction_for(stats_data, 7)
        _, entries, _ = await leaderboard_context(caller, stats_data.tournament.id, data=stats_data)
        board = leaderboard_payload(entries, 123, stats_data.tournament.id)
        board_text = rendered_payload(board).description
        name = '`lua' + '\N{NO-BREAK SPACE}' * 7 + '`'
        assert name in board_text
        await show_personal_stats(caller, 123, stats_data.tournament.id, data=stats_data)
        payload = caller.followup.send.call_args.kwargs
        card = rendered_payload(payload)
        padded_name = name
        name = '`lua`'
        assert padded_name not in card.title and padded_name not in card.description
        assert name in card.title
        assert '# __Basic Stats__' in card.description
        assert '’s __Basic Stats__' not in card.description
        assert f'whose series {name} predicts' in card.description
        assert f'whether {name} votes' in card.description
        assert '### · League rank: :challenger:' in card.description
        assert 'League rank: :challenger: Challenger' not in card.description
        assert '\n\n\u2800\n\n# __Some other Stats__' in card.description
        assert card.description.count('### · ') == 5
        assert '’s current rank:' not in card.description
        board['view'].stop()
        payload['view'].stop()
    asyncio.run(scenario())


def test_stats_insights_keep_their_content_together_with_native_dividers(stats_data):
    async def scenario():
        caller = interaction_for(stats_data, 7)
        await show_personal_stats(caller, 123, stats_data.tournament.id, data=stats_data)
        payload = caller.followup.send.call_args.kwargs
        card, = payload['view'].children
        texts = [i.content for i in card.children if isinstance(i, discord.ui.TextDisplay)]
        dividers = [i for i in card.children if isinstance(i, discord.ui.Separator)]
        assert len(texts) == 5 and len(dividers) == 4
        assert all(text.endswith("\n\n\u2800") for text in texts[1:-1])
        assert not texts[-1].endswith("\u2800")
        assert all(i.visible and i.spacing == discord.SeparatorSpacing.large for i in dividers)
        assert texts[0].endswith('# __Some other Stats__')
        assert 'Check Your PayPal' not in texts[0]
        assert isinstance(card.children[1], discord.ui.Separator) and card.children[1].visible
        labels = ['Check Your PayPal', 'Can’t Read These Frauds', 'Hopium Supplier', 'Democracy Was a Mistake']
        for text, label in zip(texts[1:], labels):
            assert '__*' + label + '*__' in text
            assert 'Not enough data' in text
            assert sum(other in text for other in labels) == 1
        assert payload['ephemeral'] is True
        payload['view'].stop()
    asyncio.run(scenario())


def test_trophycase_groups_swiss_knockout_and_overall_with_wide_gaps():
    from services.trophycase import trophycase_pages
    slots = [('swiss', str(i)) for i in range(8)] + [('quarterfinals', ''), ('semifinals', ''), ('final', ''), ('overall', '')]
    case = ['🏆'] + ['⚪'] * 7 + ['🏆', '⚪', '⚪'] + ['🏆']
    page, = trophycase_pages({7: 'lua', 8: 'Other user'}, {7: case}, slots)
    gap = '\N{NO-BREAK SPACE}' * 6
    assert ' '.join(case[:8]) + gap + ' '.join(case[8:11]) + gap + case[11] in page
    assert ' '.join(['⚪'] * 8) + gap + ' '.join(['⚪'] * 3) + gap + '⚪' in page
    assert page.count('🏆') == 4  # Three earned trophies plus the title icon.
