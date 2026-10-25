from dataclasses import replace

from services.prediction_stats import PredictionStats
from services.prediction_tiers import TIERS, prediction_tiers
from services.leaderboard_display import leaderboard_embeds


def players(count):
    return [PredictionStats(i, count - i, count, count, 0, 0) for i in range(count)]


def test_seven_players_use_guaranteed_challenger_and_proportional_remaining_slots():
    rows = players(7)
    tiers = prediction_tiers(list(reversed(rows)))
    assert list(tiers.values()) == ['challenger', 'diamond', 'diamond', 'gold', 'gold', 'gold', 'iron']


def test_ties_share_challenger_and_do_not_split_at_a_lower_boundary():
    rows = players(7)
    rows[3] = replace(rows[2], user_id=3)
    tiers = prediction_tiers(rows)
    assert tiers[2] == tiers[3] == 'diamond'
    rows[1] = replace(rows[0], user_id=1)
    tiers = prediction_tiers(rows)
    assert tiers[0] == tiers[1] == 'challenger'


def test_small_populations_and_tier_order_are_deterministic():
    order = [tier[0] for tier in TIERS]
    assert prediction_tiers([]) == {}
    assert prediction_tiers(players(1)) == {0: 'challenger'}
    for size in range(2, 60):
        tiers = prediction_tiers(players(size))
        assert len(tiers) == size
        assert list(tiers.values()).count('challenger') == 1
        indices = [order.index(tier) for tier in tiers.values()]
        assert indices == sorted(indices)


def test_group_headings_use_server_emojis_and_smaller_player_headers(monkeypatch):
    import discord
    from services import leaderboard_display
    emojis = {tier[0]: discord.PartialEmoji(name=tier[0], id=100 + i) for i, tier in enumerate(TIERS)}
    monkeypatch.setattr(leaderboard_display, 'custom_emoji', emojis.get)
    pages = leaderboard_embeds([(row, f'Player{row.user_id}') for row in players(7)])
    text = '\n'.join(page.description for page in pages)
    assert '# <:challenger:100> __Challenger (top 0.9%)__' in text
    assert '# <:diamond:101> __Diamond (top 35.9%)__' in text
    assert '# <:gold:102> __Gold (top 79.9%)__' in text
    assert '# <:iron:103> __Iron (top 100.0%)__' in text
    assert '## 1. `Player0`' in text
    assert 'First place is always Challenger' in text and 'not server percentiles' in text
    assert 'Master–Challenger' in text and 'ties share a tier' in text
