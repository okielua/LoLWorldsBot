from services.scoring_rules import RulesButton, show_rules
"""Interactive, isolated knockout rehearsal available only in development."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
import discord
from models import MatchStatus
from services import polls
from services.voting import VotingManager, VoteButton
from services.presentation import message_payload, embed_card_payload
from services.prediction_form import add_prediction_form
from services.series_results import result_pages
from services.leaderboard_display import leaderboard_embeds
from services.prediction_stats import get_competition_snapshot, get_voter_display_names
from services.mvp import trophy_cases
from services.trophycase import trophycase_pages, TrophyCasePages
from services.team_history import HistoryPages
from services.runtime_config import development_enabled


class PreviewManager(VotingManager):
    def fresh(self):
        return True  # This isolated fixture has no external schedule feed.

    async def refresh_participation(self, match_id):
        pass

    async def announce_vote(self, match_id, display_name):
        pass


class PreviewResults(HistoryPages):
    def __init__(self, pages, manager):
        super().__init__(pages)
        self.manager = manager
        rules = RulesButton()
        rules.row = 1 if len(pages) > 1 else 0
        self.add_item(rules)
        for item in (self.stats, self.trophies):
            if item not in self.children:
                self.add_item(item)
            item.row = 1 if len(pages) > 1 else 0

    @discord.ui.button(label='My Advanced Stats', emoji='🔍', style=discord.ButtonStyle.secondary)
    async def stats(self, interaction, button):
        from services.leaderboard_display import personal_stats_embed
        from services.leaderboard_views import StatsSections
        await interaction.response.defer(ephemeral=True)
        data = self.manager.store.data
        snapshot = await asyncio.to_thread(get_competition_snapshot, self.manager.guild_id, data.tournament.id, data=data)
        payload = embed_card_payload(personal_stats_embed(snapshot, interaction.user.id, interaction.user.display_name),
                                     controls=StatsSections(timeout=900), title_level=1)
        await interaction.followup.send(**payload, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='Trophy Case', emoji='🏆', style=discord.ButtonStyle.secondary)
    async def trophies(self, interaction, button):
        from services.trophycase import show_trophycase
        data = self.manager.store.data
        await show_trophycase(interaction, self.manager.guild_id, data.tournament.id, data=data, ephemeral=True)


class FinishPreview(discord.ui.Select):
    def __init__(self, manager, match, owner):
        self.manager, self.match, self.owner = manager, match, owner
        options = [discord.SelectOption(label=f'{team} wins 3–{score}', value=f'{side}:{score}')
                   for side, team in enumerate((match.team1_id, match.team2_id), 1) for score in (0, 1, 2)]
        super().__init__(placeholder='Then choose a simulated final score…', options=options, row=2)

    async def callback(self, interaction):
        if interaction.user.id != self.owner:
            await interaction.response.send_message('Run `/testknockout` for your own preview.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        manager, match = self.manager, self.match
        side, score = map(int, self.values[0].split(':'))
        finished = replace(match, status=MatchStatus.COMPLETED,
                           winner_id=match.team1_id if side == 1 else match.team2_id,
                           team1_score=3 if side == 1 else score, team2_score=score if side == 1 else 3)
        manager.store.data = replace(manager.store.data, matches=[finished if m.id == match.id else m for m in manager.store.data.matches])
        data = manager.store.data
        await asyncio.to_thread(polls.reconcile_polls, manager.guild_id, data, match.start_time + timedelta(hours=4))
        votes = polls.get_poll_results(manager.guild_id, match.id)
        pages = result_pages(finished, votes, test_mode=True, score_predictions=polls.get_score_predictions(manager.guild_id, match.id))
        pages = await add_prediction_form(pages, manager, [finished], {match.id: votes})
        await interaction.followup.send(**message_payload(pages[0], controls=PreviewResults(pages, manager)), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())
        snapshot = get_competition_snapshot(manager.guild_id, data.tournament.id, data=data)
        names = get_voter_display_names(manager.guild_id, data.tournament.id)
        cases, slots = trophy_cases(manager.guild_id, data)
        boards = leaderboard_embeds([(r, names.get(r.user_id, str(r.user_id))) for r in snapshot.rows],
                                    preview=True, history=snapshot, trophies=cases, trophy_slots=slots)
        for board in boards:
            await interaction.followup.send(**embed_card_payload(board), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        trophies = trophycase_pages(names, cases, slots)
        await interaction.followup.send(**message_payload(trophies[0], controls=TrophyCasePages(trophies)), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())


async def show_knockout_preview(interaction):
    if not development_enabled():
        await interaction.response.send_message('This preview is available in the test bot only.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    source = interaction.client.voting_manager.store.data
    template = next(m for m in source.matches if 'TBD' not in {m.team1_id, m.team2_id})
    now = datetime.now(UTC)
    token = 'knockout-test-' + uuid4().hex[:8]
    matches = [replace(template, id=f'{token}-swiss-{i}', tournament_id=token, stage_id='worlds-2026-swiss',
                       round_name='Round 1 · 0–0 record', best_of=1, start_time=now-timedelta(days=3)+timedelta(hours=i),
                       status=MatchStatus.SCHEDULED, winner_id=None, team1_score=None, team2_score=None) for i in range(8)]
    matches += [replace(template, id=f'{token}-qf-{i}', tournament_id=token, stage_id='worlds-2026-quarterfinals',
                        round_name=None, best_of=5, start_time=now+timedelta(minutes=30) if i==3 else now-timedelta(days=1,hours=3-i),
                        status=MatchStatus.SCHEDULED, winner_id=None, team1_score=None, team2_score=None) for i in range(4)]
    data = replace(source, tournament=replace(source.tournament,id=token), matches=matches)
    sample_names = {90001:'Sample Exact',90002:'Sample Close',90003:'Sample Winner'}
    for i, match in enumerate(matches):
        polls.create_poll(interaction.guild_id,match,data)
        before=min(m.start_time for m in matches)-timedelta(hours=2)
        polls.record_poll_opportunity(interaction.guild_id,match.id,match.team1_id,match.team2_id,before)
        for uid,name in sample_names.items():
            polls.cast_vote(interaction.guild_id,match.id,uid,match.team1_id,before+timedelta(minutes=1),name,
                            predicted_loser_games=(uid-90001) if match.best_of==5 else None)
        if i<len(matches)-1:
            match.status=MatchStatus.COMPLETED;match.winner_id=match.team1_id
            match.team1_score=3 if match.best_of==5 else 1;match.team2_score=0
    polls.reconcile_polls(interaction.guild_id,data,now)
    store=SimpleNamespace(data=data,fetched_at=now,last_error=None)
    manager=PreviewManager(interaction.client,store,interaction.guild_id,test_mode=True)
    poll=next(p for p in polls.get_polls(interaction.guild_id,token) if p.match_id==matches[-1].id)
    controls=discord.ui.View(timeout=900)
    for index in (1,2):
        for score in (0,1,2):
            button=VoteButton(manager,poll,index,score);button.row=index-1;controls.add_item(button)
    controls.add_item(FinishPreview(manager,matches[-1],interaction.user.id))
    await interaction.followup.send(**message_payload(
        '# Knockout Voting Test\n\nChoose your score below, then select a simulated final score. '
        'You will see the results, points leaderboard and trophy case.\n\n'
        '**3 points** exact · **2** one game off · **1** two games off · **0** wrong winner.\n\n'
        '-# Isolated sample competition; real votes, points and trophies are unchanged.',controls=controls),
        ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
