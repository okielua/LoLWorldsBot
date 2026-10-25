import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from models import MatchStatus
from services import polls
from services.prediction_form import add_prediction_form, form_rows, aligned_name_fields
from test_prediction_stats import competition, pick, complete


def test_last_five_is_oldest_to_newest_and_streak_can_exceed_five():
    recent = [SimpleNamespace(chosen='A', winner_id='A') for _ in range(7)]
    snapshot = SimpleNamespace(rows=[SimpleNamespace(user_id=7, current_streak=7)], recent={7: recent})
    line, = form_rows(snapshot, {7: 'Lua'})
    assert line.count('✅') == 5 and 'Current streak: **7** 🔥' in line
    snapshot.recent[7][0] = SimpleNamespace(chosen='B', winner_id='A')
    snapshot.rows[0].current_streak = 0
    line, = form_rows(snapshot, {7: 'Lua'})
    assert '✅ ✅ ✅ ✅ ❌' in line and 'Current streak: **0**' in line and '🔥' not in line


def test_name_fields_use_nonbreaking_padding_and_leave_emojis_outside_code():
    names = aligned_name_fields({1: 'Sam', 2: 'Taylor', 3: 'Alex'})
    assert len({len(field.strip('`')) for field in names.values()}) == 1
    assert names[1] == '`@Sam\xa0\xa0\xa0`'
    assert names[2] == '`@Taylor`'
    assert '``' in aligned_name_fields({1: 'a`b'})[1]
    snapshot = SimpleNamespace(rows=[SimpleNamespace(user_id=i,current_streak=1) for i in names],
        recent={i:[SimpleNamespace(chosen='A',winner_id='A')] for i in names})
    lines = form_rows(snapshot, {1:'Sam',2:'Taylor',3:'Alex'})
    assert all('` · ✅' in line for line in lines)


def test_short_histories_are_not_padded_with_missed_games():
    snapshot = SimpleNamespace(rows=[SimpleNamespace(user_id=7, current_streak=2)], recent={7: [
        SimpleNamespace(chosen='A', winner_id='A'), SimpleNamespace(chosen='A', winner_id='A')]})
    line, = form_rows(snapshot, {7: 'Lua'})
    assert line.count('✅') == 2 and '➖' not in line


def test_history_spans_rounds_and_later_results_do_not_rewrite_old_post(competition):
    data = competition(7)
    for match in data.matches:
        pick(match, 7)
    data = complete(data)
    manager = SimpleNamespace(guild_id=123, store=SimpleNamespace(data=data))
    last = data.matches[-1]
    votes = {last.id: [(7, last.team1_id, 'Lua')]}
    pages = ['# Results\n\nFinal score: 3–0']
    original = asyncio.run(add_prediction_form(pages, manager, [last], votes))
    assert 'Last 5 picks' in original[0] and 'Oldest → newest' in original[0]
    assert original[0].count('✅') == 6  # Five picks plus the legend.
    assert 'Current streak: **7**' in original[0]
    later = replace(last, id='later', start_time=last.start_time + timedelta(hours=1),
                    status=MatchStatus.SCHEDULED, winner_id=None)
    polls.create_poll(123, later)
    polls.record_poll_opportunity(123, later.id, later.team1_id, later.team2_id, later.start_time-timedelta(hours=1))
    pick(later, 7, False)
    later.status = MatchStatus.COMPLETED
    later.winner_id = later.team1_id
    manager.store.data = replace(data, matches=[*data.matches, later])
    polls.reconcile_polls(123, manager.store.data, later.start_time + timedelta(hours=3))
    assert asyncio.run(add_prediction_form(pages, manager, [last], votes)) == original
    new = asyncio.run(add_prediction_form(pages, manager, [later], {later.id: [(7,later.team2_id,'Lua')]}))
    assert '✅ ✅ ✅ ✅ ❌' in new[0] and 'Current streak: **0**' in new[0]


def test_large_participant_lists_stay_in_one_post_with_bounded_pages(competition):
    data = competition(1)
    for uid in range(120):
        pick(data.matches[0], uid)
    data = complete(data)
    match = data.matches[0]
    votes = {match.id: [(uid,match.team1_id,f'Player {uid}') for uid in range(120)]}
    manager = SimpleNamespace(guild_id=123,store=SimpleNamespace(data=data))
    pages = asyncio.run(add_prediction_form(['# Results\n'+ 'x'*3400],manager,[match],votes))
    assert len(pages)>1 and all(len(page)<=3600 for page in pages)
    assert sum(page.count('Current streak: **1**') for page in pages)==120
