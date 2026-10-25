import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from services.leaderboard_display import (leaderboard_embeds, sample_leaderboard_entries,
                                         personal_stats_embed, accuracy_text, UNAVAILABLE)
from services.prediction_stats import (CompetitionSnapshot, Prediction, PredictionStats,
                                      AdvancedPredictionStats, TeamPredictionStats, MinorityCall)


def test_sample_leaderboard_matches_compact_specification():
    entries = sample_leaderboard_entries("lua")
    assert len(entries) == 6
    embed, = leaderboard_embeds(entries, preview=True, previous={1: 2, 2: 1, 3: 2, 4: 5, 5: 4, 6: 6})
    text = embed.description.replace("\u00a0", "")
    assert embed.title == "TEST: Communism Worlds Leaderboard"
    assert embed.colour.value == 0x5865F2
    assert "Sample results. The real leaderboard is unchanged." in text
    assert "## 1. `lua`⬆️ 🔥\n## 18/24 (75%)" in text
    assert "## 2. `Sample Player 2`⬇️\n## 18/26 (69.2%)" in text
    assert "## 2. `Sample Player 3`" in text
    assert "## 4. `Sample Player 4`⬆️" in text
    assert '(69.2%)' in text and '(70%)' in text
    assert '## 18/24 (75%)' in text
    assert "## 6. `Sample Player 6`" in text
    assert text.index("18/24") < text.index("## 6. `Sample Player 6`")
    assert not any(word in text for word in ("participation", "75.0%", "behind", "Level on", "\u200b", "\n\n\n"))
    assert 'Current streak:' not in text and 'Best streak:' not in text
    assert '⚪' not in text and '🏆' in text and '✅' in text and '❌' in text
    assert 'Full collection' not in text and '12 trophies' not in text
    assert '**__Last 10 picks:__**' in text and '**__Trophies:__**\n🏆 (Swiss day 1)\n🏆 (Swiss day 2)' in text
    assert all("**" not in re.sub(r"\*\*\d+(?:\.\d+)?% correct\*\*", "", line) for line in text.splitlines() if line.startswith("-#"))
    assert '-# *✅ Correct · ❌ Wrong · ➖ Skipped*' in text
    assert "skipped matches don't break streaks" in text


def test_preview_has_no_actual_mentions_and_every_chunk_is_bounded():
    entries = sample_leaderboard_entries("<@123456789012345678> @everyone **lua**\nnew line")
    entries = [(replace(row, user_id=i * 6 + row.user_id), name + "x" * 200) for i in range(20) for row, name in entries]
    chunks = leaderboard_embeds(entries, preview=True)
    assert len(chunks) > 1 and all(len(embed.description) <= 4096 and len(embed) <= 6000 for embed in chunks)
    assert sum(len(re.findall(r"^## \d+\.", embed.description, re.MULTILINE)) for embed in chunks) == 120
    for embed in chunks:
        text = embed.description.replace("\u00a0", "")
        assert embed.title == "TEST: Communism Worlds Leaderboard"
        assert "<@123456789012345678>" not in text and "@everyone" not in text
        assert '-# *✅ Correct' in text and 'Ongoing streak of 3+' in text


def test_shared_first_place_and_zero_scores_do_not_gain_a_gap_or_fake_movement():
    entries = [(PredictionStats(1, 0, 1, 2, 0, 0), "A"), (PredictionStats(2, 0, 2, 2, 0, 0), "B")]
    embed, = leaderboard_embeds(entries)
    assert "## 1. `A`" in embed.description and "## 1. `B`" in embed.description
    assert "behind" not in embed.description and "Level on" not in embed.description
    assert "New" not in embed.description
    assert '⬆️' not in embed.description and '⬇️' not in embed.description
    new, = leaderboard_embeds(entries, previous={})
    assert "## 1. `A` New" in new.description


def test_full_swiss_history_skips_custom_rank_arrows_and_trophy_slots(monkeypatch):
    from types import SimpleNamespace
    import discord
    from services import leaderboard_display
    logos = {name:discord.PartialEmoji(name=name,id=100+i) for i,name in enumerate(
        ['arrow_up_blue','arrow_down_grey','dootpinkline','dot3'])}
    monkeypatch.setattr(leaderboard_display,'custom_emoji',logos.get)
    rows=[PredictionStats(1,4,5,6,4,4),PredictionStats(2,2,5,6,1,1)]
    start=datetime(2026,10,23,tzinfo=UTC)
    recent=[Prediction(f's{i}','A','B',start+timedelta(hours=i),'A' if i else 'B','A') for i in (4,3,2,0)]
    history=SimpleNamespace(rows=[PredictionStats(1,3,4,5,3,3)],recent={1:recent},
                            result_match_ids=tuple(f's{i}' for i in range(5)))
    case=['⚪','🏆']+['⚪']*10
    embed,=leaderboard_embeds([(rows[0],'Winner'),(rows[1],'Other')],previous={1:2,2:1},
        history=history,trophies={1:case},trophy_slots=[('swiss', str(i)) for i in range(8)] + [('quarterfinals', ''), ('semifinals', ''), ('final', ''), ('overall', '')])
    text=embed.description.replace("\u00a0", "")
    assert '`Winner`<:arrow_up_blue:100> 🔥\n## 4/5 (80%)' in text
    assert '`Other`<:arrow_down_grey:101>\n## 2/5 (40%)' in text
    assert '❌ <:dootpinkline:102> ✅ ✅ ✅' in text
    assert '\n🏆' in text and '<:dot3:103>' not in text and '⚪' not in text
    assert 'Current streak:' not in text and 'Best streak:' not in text and 'Oldest' not in text


def test_personal_stats_use_requested_summary_and_four_explained_sections():
    row = PredictionStats(4, 14, 20, 30, 2, 5)
    leader = PredictionStats(1, 18, 24, 30, 4, 7)
    start = datetime(2026, 10, 16, tzinfo=UTC)
    recent = [Prediction(f"m{i}", "C9", "FUR", start - timedelta(hours=i), "FUR", "C9" if i % 2 else "FUR") for i in range(12)]
    pending = [replace(prediction, winner_id=None) for prediction in recent[:7]]
    snapshot = CompetitionSnapshot([leader, row], None, {4: recent}, {4: pending}, 30)
    embed = personal_stats_embed(snapshot, 4, "Sample Player 4", previous={1: 1, 4: 3})
    text = embed.description.replace("\u00a0", "")
    assert embed.title == "My Worlds Stats · Sample Player 4"
    assert "**My rank: 2 of 2**\n**My correct predictions: 14 (70%)**" in text
    assert "My votes: **20 of 30 past matches**" in text
    assert "My incorrect predictions: **6**\nMy missed votes: **10**" in text
    assert "My current streak: **2**\nMy best streak: **5**" in text
    assert text.index("## :PeyzPal: __Check Your PayPal__") < text.index("## :PepeKMS: __Can’t Read These Frauds__") < text.index("## :Copium: __Hopium Supplier__") < text.index("## :Discheesed: __Democracy Was a Mistake__")
    assert text.startswith("-# *(All stats cover completed series across Worlds, including Play-Ins.)*")
    assert not any(label in text for label in ("Recent predictions", "Pending votes", "Boldest correct call", "Ranked by", "Rank movement", "behind the leader"))
    notes = [line for line in text.splitlines() if line.startswith("-# ")]
    assert len(notes) == 5 and all(line.startswith("-# *") and line.endswith("*") for line in notes)
    assert "•" not in text and "\n- " not in text and "## Advanced stats" not in text
    assert len(embed.description) <= 4096 and len(embed) <= 6000


def test_pending_only_and_no_votes_have_clear_empty_states():
    prediction = Prediction("m", "C9", "FUR", datetime(2026, 10, 17, tzinfo=UTC), "C9", None)
    snapshot = CompetitionSnapshot([], None, {}, {7: [prediction]}, 30)
    embed = personal_stats_embed(snapshot, 7, "lua")
    assert f"**My rank: {UNAVAILABLE}**" in embed.description
    assert "Pending votes" not in embed.description and "**My correct predictions: 0 (—)**" in embed.description
    assert "accuracy" not in embed.description.lower()
    assert accuracy_text(PredictionStats(7, 0, 0, 30, 0, 0)) == UNAVAILABLE
    empty = personal_stats_embed(snapshot, 8, "No votes")
    assert empty.description.count("Not enough data") == 4
    snapshot = replace(snapshot, rows=[PredictionStats(7, 1, 1, 30, 1, 1)], pending={})
    assert "**My rank: 1 of 1**" in personal_stats_embed(snapshot, 7, "lua").description


def test_renamed_insights_show_real_values_below_explanations():
    start = datetime(2026, 10, 16, tzinfo=UTC)
    prediction = Prediction("m", "T1", "C9", start, "T1", "T1")
    advanced = AdvancedPredictionStats(
        TeamPredictionStats("T1", 4, 5, 3, 3),
        TeamPredictionStats("GEN", 1, 3, 2, 0),
        TeamPredictionStats("G2", 3, 5, 4, 3),
        2, 3, MinorityCall(prediction, 2, 8),
        hopium_supplier=TeamPredictionStats("GEN", 1, 3, 2, 0),
    )
    snapshot = CompetitionSnapshot([PredictionStats(7, 8, 12, 15, 2, 4)], [], {7: [prediction]}, {}, 15, {7: advanced})
    embed = personal_stats_embed(snapshot, 7, "lua")
    text = embed.description.replace("\u00a0", "")
    assert "\n\n**T1** · **4/5 correct (80%)**" in text
    assert "\n\n**GEN** · **1/3 correct (33.3%)**" in text
    assert "\n\n**GEN** · **backed to win in 2 series they lost**" in text
    assert text.endswith("**2/3 minority predictions correct**")
    assert "## :PeyzPal: __Check Your PayPal__\n\n-# *The team whose series I predict most accurately" in text
    assert "## :PepeKMS: __Can’t Read These Frauds__\n\n-# *The team whose series I predict least accurately" in text
    assert "## :Copium: __Hopium Supplier__\n\n-# *The team I backed to win most often when they actually lost." in text
    assert "## :Discheesed: __Democracy Was a Mistake__\n\n-# *How often I predict correctly" in text
    assert "Boldest correct call" not in text and "Recent predictions" not in text


def test_advanced_stats_show_minimums_and_remain_within_the_actual_card_limit_with_logos():
    from services.team_emojis import configure_team_emojis
    from services.presentation import embed_card_payload
    import discord
    import asyncio

    async def scenario():
        start = datetime(2026, 10, 16, tzinfo=UTC)
        predictions = [Prediction(f"m{i}", "T1", "C9", start - timedelta(hours=i), "T1", "T1") for i in range(30)]
        insight = AdvancedPredictionStats(TeamPredictionStats("T1", 4, 5, 3, 3), TeamPredictionStats("C9", 4, 5, 2, 1),
                                          TeamPredictionStats("T1", 4, 5, 3, 3), 2, 3, MinorityCall(predictions[0], 2, 8))
        snapshot = CompetitionSnapshot([PredictionStats(7, 20, 30, 40, 2, 4)], [], {7: predictions}, {7: predictions}, 40, {7: insight})
        configure_team_emojis([discord.PartialEmoji(name="t1", id=1559495487462056007), discord.PartialEmoji(name="CLOUD9", id=1559495722594992178)])
        try:
            embed = personal_stats_embed(snapshot, 7, "*long name* " * 40)
            payload = embed_card_payload(embed)
            text = "\n".join(item.content for item in payload["view"].walk_children() if isinstance(item, discord.ui.TextDisplay))
            assert len(text) <= 4000
            assert all(label in text for label in ("## :PeyzPal: __Check Your PayPal__", "## :PepeKMS: __Can’t Read These Frauds__", "## :Copium: __Hopium Supplier__", "## :Discheesed: __Democracy Was a Mistake__"))
            assert "<:t1:" in text and "<:CLOUD9:" in text
            assert "Pending votes" not in text
            payload["view"].stop()
        finally:
            configure_team_emojis(())
        pending = CompetitionSnapshot([], None, {}, {7: predictions[:1]}, 40)
        text = personal_stats_embed(pending, 7, "lua").description
        assert "at least 3 scored predictions involving that team" in text
        assert "Not enough data" in text
    asyncio.run(scenario())


def test_personal_section_headers_use_real_custom_emojis_in_requested_order():
    import discord
    from services.team_emojis import configure_team_emojis
    names = ["PeyzPal", "PepeKMS", "Copium", "Discheesed"]
    labels = ["Check Your PayPal", "Can’t Read These Frauds", "Hopium Supplier", "Democracy Was a Mistake"]
    emojis = [discord.PartialEmoji(name=name, id=1559495487462056007 + index) for index, name in enumerate(names)]
    configure_team_emojis(emojis)
    try:
        snapshot = CompetitionSnapshot([], None, {}, {}, 0)
        text = personal_stats_embed(snapshot, 7, "lua").description
        headers = [line for line in text.splitlines() if line.startswith("## ")]
        assert headers == [f"## {emoji} __{label}__" for emoji, label in zip(emojis, labels)]
        assert "Advanced stats" not in text and "•" not in text
        notes = [line for line in text.splitlines() if line.startswith("-# ")]
        assert all(line.startswith("-# *") and line.endswith("*") for line in notes)
    finally:
        configure_team_emojis(())


def test_history_only_displays_latest_ten_matches_without_changing_totals():
    from types import SimpleNamespace
    row = PredictionStats(1, 11, 18, 18, 1, 5)
    start = datetime(2026, 10, 23, tzinfo=UTC)
    picks = [Prediction(f's{i}', 'A', 'B', start + timedelta(hours=i),
                        'A' if i >= 8 else 'B', 'A') for i in range(18)]
    history = SimpleNamespace(rows=[row], recent={1: list(reversed(picks))},
                              result_match_ids=tuple(f's{i}' for i in range(18)))
    embed, = leaderboard_embeds([(row, 'Player')], history=history)
    assert '## 11/18 (61.1%)' in embed.description
    assert ' '.join(['✅'] * 10) in embed.description
    assert '❌' not in embed.description.split('-# *Standings')[0]
    assert 'last 10 picks shown' in embed.description


def test_earned_trophies_are_labelled_by_matchday_and_stage():
    row = PredictionStats(1, 11, 19, 19, 0, 5)
    slots = [('swiss', str(i)) for i in range(8)] + [('quarterfinals', ''), ('semifinals', ''), ('final', ''), ('overall', '')]
    case = ['⚪'] * 12
    for i in (1, 8, 11):
        case[i] = '🏆'
    embed, = leaderboard_embeds([(row, 'Player')], trophies={1: case}, trophy_slots=slots)
    assert '**__Trophies:__**\n🏆 (Swiss day 2)\n🏆 (Quarterfinals)\n🏆 (Worlds 2026 overall)' in embed.description
    assert '⚪' not in embed.description and 'Full collection' not in embed.description


def test_leaderboard_header_name_blocks_share_the_longest_width():
    rows = [(PredictionStats(1, 2, 3, 4, 0, 2), 'Amy'),
            (PredictionStats(2, 1, 3, 4, 0, 1), 'TheFluke')]
    embed, = leaderboard_embeds(rows, previous={1:2, 2:1})
    assert '## 1. `Amy' + '\N{NO-BREAK SPACE}' * 5 + '`⬆️' in embed.description
    assert '## 2. `TheFluke`⬇️' in embed.description
