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
    text = embed.description
    assert embed.title == "TEST: Communism Worlds Leaderboard"
    assert embed.colour.value == 0x5865F2
    assert "Sample results. The real leaderboard is unchanged." in text
    assert "### `1.` lua: 18 correct\n\n-# *Rank movement: ↑1*\n-# *Voted on 24 of 30 past matches*\n-# *Accuracy: **75% correct***\n\n-# *🔥 Current streak: 4*\n-# *Best streak: 7*" in text
    assert "### `2.` Sample Player 2: 18 correct\n\n-# *Rank movement: ↓1*\n" in text
    assert "### `2.` Sample Player 3: 18 correct\n" in text
    assert "### `4.` Sample Player 4: 14 correct\n\n-# *Rank movement: ↑1*\n" in text
    assert "**69.2% correct**" in text and "**70% correct**" in text
    assert "### `6.` Sample Player 6: 0 correct\n" in text
    assert text.index("18 correct") < text.index("0 correct")
    assert not any(word in text for word in ("participation", "75.0%", "behind", "Level on", "\u200b", "\n\n\n"))
    assert "🔥 Current streak: 3" in text
    assert "🔥 Current streak: 2" not in text and "🔥 Current streak: 0" not in text
    assert all("**" not in re.sub(r"\*\*\d+(?:\.\d+)?% correct\*\*", "", line) for line in text.splitlines() if line.startswith("-#"))
    assert "-# • *Ranked by correct predictions, then accuracy.*" in text
    assert "missed votes don’t" in text


def test_preview_has_no_actual_mentions_and_every_chunk_is_bounded():
    entries = sample_leaderboard_entries("<@123456789012345678> @everyone **lua**\nnew line")
    entries = [(replace(row, user_id=i * 6 + row.user_id), name + "x" * 200) for i in range(20) for row, name in entries]
    chunks = leaderboard_embeds(entries, preview=True)
    assert len(chunks) > 1 and all(len(embed.description) <= 4096 and len(embed) <= 6000 for embed in chunks)
    assert sum(len(re.findall(r"^### `\d+\.`", embed.description, re.MULTILINE)) for embed in chunks) == 120
    for embed in chunks:
        text = embed.description
        assert embed.title == "TEST: Communism Worlds Leaderboard"
        assert "<@123456789012345678>" not in text and "@everyone" not in text
        assert "-# • *Ranked by" in text and "-# • *Incorrect predictions" in text


def test_shared_first_place_and_zero_scores_do_not_gain_a_gap_or_fake_movement():
    entries = [(PredictionStats(1, 0, 1, 2, 0, 0), "A"), (PredictionStats(2, 0, 2, 2, 0, 0), "B")]
    embed, = leaderboard_embeds(entries)
    assert "### `1.` A: 0 correct" in embed.description and "### `1.` B: 0 correct" in embed.description
    assert "behind" not in embed.description and "Level on" not in embed.description
    assert "New" not in embed.description
    assert "↑" not in embed.description.split("-# • *Ranked")[0]
    new, = leaderboard_embeds(entries, previous={})
    assert "### `1.` A: 0 correct\n\n-# *Rank movement: New*" in new.description


def test_personal_stats_use_requested_summary_and_four_explained_sections():
    row = PredictionStats(4, 14, 20, 30, 2, 5)
    leader = PredictionStats(1, 18, 24, 30, 4, 7)
    start = datetime(2026, 10, 16, tzinfo=UTC)
    recent = [Prediction(f"m{i}", "C9", "FUR", start - timedelta(hours=i), "FUR", "C9" if i % 2 else "FUR") for i in range(12)]
    pending = [replace(prediction, winner_id=None) for prediction in recent[:7]]
    snapshot = CompetitionSnapshot([leader, row], None, {4: recent}, {4: pending}, 30)
    embed = personal_stats_embed(snapshot, 4, "Sample Player 4", previous={1: 1, 4: 3})
    text = embed.description
    assert embed.title == "My Worlds Stats · Sample Player 4"
    assert "**My rank: 2 of 2**\n**My correct predictions: 14**" in text
    assert "My votes: **20 of 30 past matches**\nMy accuracy: **70% correct**" in text
    assert "My incorrect predictions: **6**\nMy missed votes: **10**" in text
    assert "My current streak: **2**\nMy best streak: **5**" in text
    assert text.index("## :PeyzPal: __Check Your PayPal__") < text.index("## :PepeKMS: __Can’t Read These Frauds__") < text.index("## :Copium: __Hopium Supplier__") < text.index("## :Discheesed: __Democracy Was a Mistake__")
    assert text.startswith("-# *(All stats cover completed series from Swiss Stage onward.)*")
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
    assert "Pending votes" not in embed.description and "**My correct predictions: 0**" in embed.description
    assert f"past matches**\nMy accuracy: **{UNAVAILABLE}**" in embed.description
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
    text = embed.description
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
