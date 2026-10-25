import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from models import MatchStatus
from services import mvp, polls, user_settings
from services.tournament_data import load_tournament_data


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, 'DATABASE_FILE', tmp_path / 'mvp.sqlite')
    user_settings.initialize_database()
    polls.initialize_polls()
    return load_tournament_data(Path(__file__).resolve().parents[1] / 'data/worlds_2026.json')


def period(source, stage, count, start, *, round_number=None, offset=0):
    return [replace(source.matches[0], id=f'{stage}-{round_number}-{i}', stage_id=f'worlds-2026-{stage}',
                    best_of=1 if round_number else 5,
                    round_name=f'Round {round_number} · 0–0 record' if round_number else None,
                    start_time=start + timedelta(hours=i, days=offset if i >= count // 2 else 0))
            for i in range(count)]


def populate(data, choices):
    before = min(m.start_time for m in data.matches) - timedelta(hours=1)
    for match in data.matches:
        polls.create_poll(123, match, data)
        polls.record_poll_opportunity(123, match.id, match.team1_id, match.team2_id, before)
        for user, correct in choices(match):
            polls.cast_vote(123, match.id, user, match.team1_id if correct else match.team2_id,
                            before + timedelta(minutes=1), f'Player {user}')


def finish(data, matches):
    for match in matches:
        match.status = MatchStatus.COMPLETED
        match.winner_id = match.team1_id
        match.team1_score, match.team2_score = match.best_of // 2 + 1, 0
    polls.reconcile_polls(123, data, max(m.start_time for m in data.matches) + timedelta(hours=3))


def test_awards_are_scoped_to_each_swiss_round_and_knockout_stage(source):
    first = period(source, 'swiss', 8, datetime(2026, 10, 23, 17, tzinfo=UTC), round_number=1)
    second = period(source, 'swiss', 8, datetime(2026, 10, 24, 17, tzinfo=UTC), round_number=2)
    quarter = period(source, 'quarterfinals', 4, datetime(2026, 11, 1, 17, tzinfo=UTC))
    playin = period(source, 'play-in', 1, datetime(2026, 10, 18, 17, tzinfo=UTC))
    data = replace(source, matches=playin + first + second + quarter)
    def choices(match):
        if match in first: return [(7, True), (8, False)]
        if match in second: return [(7, False), (8, True)]
        return [(9, True)]
    populate(data, choices)
    finish(data, data.matches)
    awards = mvp.get_mvp_awards(123, data)
    assert [a.label.split(' ·')[0] for a in awards] == ['Swiss Round 1', 'Swiss Round 2', 'Quarterfinals']
    assert [[row.user_id for row in a.winners] for a in awards] == [[7], [8], [9]]
    assert [a.winners[0].correct for a in awards] == [8, 8, 4]


def test_two_day_swiss_round_has_separate_daily_awards_and_shared_ties(source):
    matches = period(source, 'swiss', 8, datetime(2026, 10, 25, 17, tzinfo=UTC), round_number=3, offset=1)
    data = replace(source, matches=matches)
    populate(data, lambda _: [(7, True), (8, True)])
    finish(data, matches[:4])
    first_day, = mvp.get_mvp_awards(123, data)
    assert 'Sunday, 25 October' in first_day.label
    assert {r.user_id for r in first_day.winners} == {7, 8}
    assert all(r.correct == 4 for r in first_day.winners)
    finish(data, matches)
    first_day, award = mvp.get_mvp_awards(123, data)
    assert {r.user_id for r in award.winners} == {7, 8}
    assert all(r.correct == 4 for r in award.winners)
    assert 'Sunday' in mvp.previous_mvp_award(123, data, '2026-10-26').label
    assert 'Monday' in mvp.previous_mvp_award(123, data, '2026-10-27').label
    pages = mvp.mvp_pages([award], {7: 'Lua', 8: 'Jerry'})
    assert '## 🏆 Lua' in pages[0] and '## 🏆 Jerry' in pages[0]
    assert '4/4 correct' in pages[0]


def test_results_place_matchday_mvp_after_form_and_keep_days_separate(source):
    from services.prediction_form import add_prediction_form
    matches = period(source, 'swiss', 8, datetime(2026, 10, 25, 17, tzinfo=UTC), round_number=3, offset=1)
    data = replace(source, matches=matches)
    populate(data, lambda match: [(7,match in matches[:4]), (8,match in matches[4:])])
    finish(data,matches[:4])
    manager = SimpleNamespace(guild_id=123,store=SimpleNamespace(data=data))
    votes = {match.id:polls.get_poll_results(123,match.id) for match in matches[:4]}
    pages = asyncio.run(add_prediction_form(['# Results'],manager,matches[:4],votes))
    text='\n'.join(pages)
    assert text.index('__Last 5 picks__') < text.index('Matchday MVP') < text.index('🏆 Player 7')
    assert '## __Matchday MVP · Sunday, 25 October 2026__' in text
    finish(data,matches[4:])
    votes = {match.id:polls.get_poll_results(123,match.id) for match in matches}
    pages = asyncio.run(add_prediction_form(['# Results'],manager,matches,votes))
    text='\n'.join(pages)
    assert text.count('## __Matchday MVP')==2
    assert '🏆 Player 7' in text and '🏆 Player 8' in text


def test_accuracy_breaks_equal_correct_counts_and_missing_fixtures_do_not_award(source):
    matches = period(source, 'quarterfinals', 4, datetime(2026, 11, 1, 17, tzinfo=UTC))
    data = replace(source, matches=matches)
    populate(data, lambda match: [(7, match in matches[:2])] + ([(8, True)] if match in matches[:2] else []))
    finish(data, matches)
    award, = mvp.get_mvp_awards(123, data)
    assert [r.user_id for r in award.winners] == [8]
    assert mvp.get_mvp_awards(123, replace(data, matches=matches[:3])) == []


def test_mvp_command_uses_big_headings_and_never_pings(source, monkeypatch):
    matches = period(source, 'swiss', 8, datetime(2026, 10, 23, 17, tzinfo=UTC), round_number=1)
    data = replace(source, matches=matches)
    populate(data, lambda _: [(7, True)])
    finish(data, matches)
    caller = SimpleNamespace(guild_id=123, guild=SimpleNamespace(get_member=lambda _: None),
        client=SimpleNamespace(voting_manager=SimpleNamespace(guild_id=123, store=SimpleNamespace(data=data))),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(mvp.show_mvps(caller))
    caller.response.defer.assert_awaited_once()
    payload = caller.followup.send.call_args.kwargs
    text = str(payload['view'].to_components())
    assert '## 🏆 Player 7' in text and 'Swiss Round 1' in text
    assert payload['allowed_mentions'].to_dict() == {'parse': []}


def test_twelve_trophy_slots_cover_matchdays_stages_and_overall(source):
    first=period(source,'swiss',8,datetime(2026,10,23,17,tzinfo=UTC),round_number=1)
    second=period(source,'swiss',8,datetime(2026,10,24,17,tzinfo=UTC),round_number=2)
    third=period(source,'swiss',8,datetime(2026,10,25,17,tzinfo=UTC),round_number=3,offset=1)
    fourth=period(source,'swiss',6,datetime(2026,10,28,17,tzinfo=UTC),round_number=4)
    for i,match in enumerate(fourth):match.start_time=datetime(2026,10,28+i//2,17+i%2,tzinfo=UTC)
    fifth=period(source,'swiss',3,datetime(2026,10,31,17,tzinfo=UTC),round_number=5)
    quarters=period(source,'quarterfinals',4,datetime(2026,11,1,17,tzinfo=UTC))
    semis=period(source,'semifinals',2,datetime(2026,11,8,17,tzinfo=UTC))
    final=period(source,'final',1,datetime(2026,11,14,17,tzinfo=UTC))
    data=replace(source,matches=first+second+third+fourth+fifth+quarters+semis+final)
    populate(data,lambda _:[(7,True),(8,True)])
    finish(data,first)
    cases,slots=mvp.trophy_cases(123,data)
    assert len(slots)==12 and sum(key[0]=='swiss' for key in slots)==8
    assert cases[7]==['🏆']+['⚪']*11 and cases[8]==cases[7]
    finish(data,data.matches[:-1])
    cases,slots=mvp.trophy_cases(123,data)
    assert slots[-1] == ('overall', '') and cases[7][-1] == '⚪'
    finish(data,data.matches)
    cases,slots=mvp.trophy_cases(123,data)
    assert cases[7]==cases[8]==['🏆']*12
