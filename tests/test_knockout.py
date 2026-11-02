import asyncio
from dataclasses import replace
from datetime import datetime, UTC, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import discord
import pytest
from models import MatchStatus
from services import polls
from services.knockout import prediction_points, publication_time
from services.prediction_stats import get_competition_snapshot, competition_ranks
from services.voting import VotingManager, vote_button_groups, ScoreVoteButton
from test_voting import context


def knockout(context):
    manager, store, channel, now, message = context
    match = replace(store.data.matches[0], stage_id='worlds-2026-quarterfinals', best_of=5,
                    start_time=datetime(2026,11,3,22,tzinfo=UTC), round_name=None)
    store.data=replace(store.data,matches=[match])
    now[0]=datetime(2026,11,3,3,tzinfo=UTC);store.fetched_at=now[0]
    return match


@pytest.mark.parametrize('actual',[0,1,2])
@pytest.mark.parametrize('predicted',[0,1,2])
def test_score_matrix_rewards_correct_winner_first(context,actual,predicted):
    match=knockout(context)
    match=replace(match,status=MatchStatus.COMPLETED,winner_id=match.team1_id,team1_score=3,team2_score=actual)
    assert prediction_points(match,match.team1_id,predicted)==3-abs(actual-predicted)
    assert prediction_points(match,match.team2_id,predicted)==0
    assert prediction_points(match,match.team1_id,None)==1


def test_four_am_publication_recovery_and_restart_do_not_duplicate(context):
    match=knockout(context);manager,store,channel,now,_=context
    async def scenario():
        now[0]=publication_time(match)-timedelta(seconds=1);store.fetched_at=now[0]
        await manager.tick();channel.send.assert_not_awaited()
        now[0]+=timedelta(seconds=2);store.fetched_at=now[0]
        await manager.tick();channel.send.assert_awaited_once()
        payload=channel.send.call_args.kwargs
        buttons=[getattr(i,'item',i) for i in payload['view'].walk_children()]
        scores=[b for b in buttons if isinstance(b,discord.ui.Button) and b.custom_id.startswith('worldsscore:')]
        assert len(scores)==6 and len({b.custom_id for b in scores})==6
        restarted=VotingManager(manager.client,store,123,clock=lambda:now[0])
        await restarted.tick();channel.send.assert_awaited_once()
    asyncio.run(scenario())


def test_score_votes_change_persist_and_cutoff_then_score_without_source(context):
    match=knockout(context);manager,store,channel,now,_=context
    async def scenario():
        await manager.tick()
        with pytest.raises(ValueError,match='exact score'):
            await manager.vote(match.id,7,match.team1_id)
        await manager.vote(match.id,7,match.team1_id,'Exact',predicted_loser_games=0)
        await manager.vote(match.id,7,match.team1_id,'Exact',predicted_loser_games=1)
        await manager.vote(match.id,8,match.team1_id,'Close',predicted_loser_games=2)
        await manager.vote(match.id,9,match.team2_id,'Wrong',predicted_loser_games=1)
        assert polls.get_score_predictions(123,match.id)=={7:1,8:2,9:1}
        poll=polls.get_polls(123,store.data.tournament.id)[0]
        button=vote_button_groups(manager,poll)[0][1]
        parsed=ScoreVoteButton.__discord_ui_compiled_template__.fullmatch(button.item.custom_id)
        rebuilt=await ScoreVoteButton.from_custom_id(SimpleNamespace(client=SimpleNamespace(voting_manager=manager)),button.item,parsed)
        assert rebuilt.item.losing_games==1 and rebuilt.item.team_id==match.team1_id
        from services.voting import PersonalVoteView
        private=PersonalVoteView(manager,poll,7,match.team1_id)
        assert len({b.custom_id for b in private.children})==6
        selected=[b for b in private.children if b.style==discord.ButtonStyle.success]
        assert len(selected)==1 and selected[0].losing_games==1
        private.stop()
        now[0]=match.start_time-timedelta(minutes=5);store.fetched_at=now[0]
        with pytest.raises(ValueError):await manager.vote(match.id,7,match.team1_id,predicted_loser_games=0)
        finished=replace(match,status=MatchStatus.COMPLETED,winner_id=match.team1_id,team1_score=3,team2_score=1)
        store.data=replace(store.data,matches=[finished]);polls.reconcile_polls(123,store.data,match.start_time+timedelta(hours=3))
        snapshot=get_competition_snapshot(123,store.data.tournament.id,data=store.data)
        assert [(r.user_id,r.total_points,r.pre_knockout_submitted) for r in snapshot.rows]==[(7,3,0),(8,2,0),(9,0,0)]
        assert get_competition_snapshot(123,store.data.tournament.id).rows==snapshot.rows
        assert competition_ranks(snapshot.rows)=={7:1,8:2,9:3}
        from services.series_results import result_pages
        text='\n'.join(result_pages(finished,polls.get_poll_results(123,match.id),score_predictions=polls.get_score_predictions(123,match.id)))
        assert '3–1 · 3 points' in text and '3–2 · 2 points' in text and '@Exact' in text
    asyncio.run(scenario())


def test_interactive_preview_is_isolated_and_can_finish(context,monkeypatch):
    from services.knockout_preview import show_knockout_preview,FinishPreview
    from services import user_settings
    manager,store,channel,now,_=context
    caller=SimpleNamespace(guild_id=123,user=SimpleNamespace(id=7,display_name='Tester'),
        client=SimpleNamespace(voting_manager=manager),response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
    monkeypatch.setenv('BOT_MODE','development')
    async def scenario():
        await show_knockout_preview(caller)
        view=caller.followup.send.call_args.kwargs['view']
        finish=next(i for i in view.walk_children() if isinstance(i,FinishPreview))
        assert finish.manager.store.data.tournament.id!=store.data.tournament.id
        poll=next(p for p in polls.get_polls(123,finish.manager.store.data.tournament.id) if p.match_id==finish.match.id)
        await finish.manager.vote(poll.match_id,7,poll.team1_id,'Tester',predicted_loser_games=1)
        finish._values=['1:1']
        await finish.callback(caller)
        text=str([c.kwargs['view'].to_components() for c in caller.followup.send.call_args_list])
        assert 'Quarterfinals Trophy Winner' in text and 'Knockout prediction points' in text
        assert not polls.get_polls(123,store.data.tournament.id)
        for call in caller.followup.send.call_args_list:call.kwargs['view'].stop()
    asyncio.run(scenario())


def test_incomplete_knockout_result_does_not_award_points(context):
    match=knockout(context);manager,store,channel,now,_=context
    async def scenario():
        await manager.tick()
        await manager.vote(match.id,7,match.team1_id,'Tester',predicted_loser_games=1)
        finished=replace(match,status=MatchStatus.COMPLETED,winner_id=match.team1_id,team1_score=None,team2_score=None)
        store.data=replace(store.data,matches=[finished])
        polls.reconcile_polls(123,store.data,match.start_time+timedelta(hours=3))
        assert not get_competition_snapshot(123,store.data.tournament.id,data=store.data).rows
        assert not get_competition_snapshot(123,store.data.tournament.id).rows
    asyncio.run(scenario())
