import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import discord

from models import MatchStatus
from services.match_browser import show_match_games
from services.tournament_data import load_tournament_data
from services.team_history import HistoryPages


@pytest.fixture
def browser(monkeypatch):
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json")
    now = datetime(2026, 10, 18, tzinfo=UTC)
    monkeypatch.setattr("services.match_browser.get_registered_timezones", lambda: ["Europe/Berlin"])
    interaction = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=AsyncMock()))
    return data, now, interaction


@pytest.mark.parametrize("upcoming", [False, True])
@pytest.mark.parametrize("requested", [3, 15, 100])
def test_clamps_to_available_matches_and_pages_in_correct_order(browser, upcoming, requested):
    data, now, interaction = browser
    matches = [replace(data.matches[0], id=f"fixture-{i}",
                       status=MatchStatus.SCHEDULED if upcoming else MatchStatus.COMPLETED,
                       start_time=now + timedelta(hours=i + 1) * (1 if upcoming else -1)) for i in range(12)]
    ignored = [replace(matches[0], id="live", status=MatchStatus.LIVE),
               replace(matches[0], id="cancelled", status=MatchStatus.CANCELLED),
               replace(matches[0], id="wrong-status", status=MatchStatus.COMPLETED if upcoming else MatchStatus.SCHEDULED)]
    data = replace(data, matches=list(reversed(matches)) + ignored)
    rendered = []

    def formatter(match, timezone, **options):
        rendered.append(match.id)
        assert options["hide_result"] is True
        assert options["timezones"] == ["Europe/Berlin"]
        return f"Match {match.id}"

    async def scenario():
        await show_match_games(interaction, data, formatter, count=requested, upcoming=upcoming, now=now)
        assert rendered == [match.id for match in matches[:requested]]
        sent = interaction.response.send_message.call_args.kwargs
        assert not sent.get("ephemeral", False)
        controls = sent["view"].controls
        for index, page in enumerate(controls.pages, 1):
            page_label = f" (Page {index}/{len(controls.pages)})" if len(controls.pages) > 1 else ""
            assert page.startswith(f"# {'Next' if upcoming else 'Previous'} {min(requested, 12)} Matches{page_label}\n")
            assert "-# Page " not in page
        assert len(controls.pages) == (min(requested, 12) + 2) // 3
        assert all(page.count("Match fixture-") == 3 for page in controls.pages)
        note = f"Only 12 {'upcoming' if upcoming else 'completed'} matches are available."
        assert (note in controls.pages[0]) is (requested > 12)
        if requested > 12:
            assert controls.previous.disabled and not controls.next_page.disabled
            click = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
            for _ in range(10):
                await controls.turn(click, 1)
            assert controls.page == 3 and controls.next_page.disabled
            assert "fixture-11" in controls.pages[controls.page]
            for _ in range(10):
                await controls.turn(click, -1)
            assert controls.page == 0 and controls.previous.disabled
        else:
            assert controls.children == [controls.more]
            assert controls.more.label == f"See 3 More {'Next' if upcoming else 'Previous'} Matches"
            assert controls.more.style == discord.ButtonStyle.primary
    asyncio.run(scenario())


@pytest.mark.parametrize("upcoming", [False, True])
@pytest.mark.parametrize("available,initial", [(8, 3), (105, 99)])
def test_see_more_extends_three_at_a_time_updates_title_and_stops(browser, upcoming, available, initial):
    data, now, interaction = browser
    matches = [replace(data.matches[0], id=f"fixture-{i}",
                       status=MatchStatus.SCHEDULED if upcoming else MatchStatus.COMPLETED,
                       start_time=now + timedelta(hours=i + 1) * (1 if upcoming else -1)) for i in range(available)]
    data = replace(data, matches=matches)

    async def scenario():
        await show_match_games(interaction, data, lambda match, *a, **kw: f"Match {match.id}",
                               count=initial, upcoming=upcoming, now=now)
        controls = interaction.response.send_message.call_args.kwargs["view"].controls
        shown = initial
        click = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        while controls.has_more:
            await controls.more.callback(click)
            first_new_page = shown // 3
            shown = min(shown + 3, available, 100)
            assert controls.page == first_new_page
            assert controls.pages[0].startswith(f"# {'Next' if upcoming else 'Previous'} {shown} Matches (Page 1/{len(controls.pages)})\n")
            assert f"(Page {controls.page + 1}/{len(controls.pages)})\n" in controls.pages[controls.page]
            text = "\n".join(controls.pages)
            assert text.count("Match fixture-") == shown
            assert len(controls.pages) == (shown + 2) // 3
            assert click.response.edit_message.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert shown == min(available, 100)
        assert controls.more not in controls.children
        assert controls.next_page.disabled
        assert "fixture-0" in controls.pages[0]
        await controls.turn(click, -1)
        assert controls.page == len(controls.pages) - 2
    asyncio.run(scenario())


@pytest.mark.parametrize("upcoming", [False, True])
def test_empty_schedule_returns_clear_message(browser, upcoming):
    data, now, interaction = browser
    asyncio.run(show_match_games(interaction, replace(data, matches=[]), None, count=3, upcoming=upcoming, now=now))
    payload = interaction.response.send_message.call_args.kwargs
    content = payload["view"].children[0].children[0].content
    assert ("No upcoming matches" if upcoming else "No completed Worlds matches yet") in content


@pytest.mark.parametrize("count", [0, -1, 1000, 1.5, "15", True])
def test_invalid_count_is_rejected_without_rendering(browser, count):
    data, now, interaction = browser
    asyncio.run(show_match_games(interaction, data, None, count=count, upcoming=True, now=now))
    payload = interaction.response.send_message.call_args.kwargs
    assert payload["ephemeral"]
    assert "whole number from 1 to 100" in payload["view"].children[0].children[0].content


def test_other_users_cannot_turn_someone_elses_pages(browser, monkeypatch):
    from services.match_browser import MatchPages
    monkeypatch.setattr(HistoryPages, "interaction_check", AsyncMock(return_value=True))

    async def scenario():
        controls = MatchPages(["Page one", "Page two"], 7, "nextgames")
        click = SimpleNamespace(user=SimpleNamespace(id=8), response=SimpleNamespace(send_message=AsyncMock()))
        assert not await controls.interaction_check(click)
        assert controls.page == 0
        assert click.response.send_message.call_args.kwargs["ephemeral"]
        click.user.id = 7
        assert await controls.interaction_check(click)
    asyncio.run(scenario())
