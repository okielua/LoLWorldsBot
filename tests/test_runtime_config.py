import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import discord
import pytest

from services.runtime_config import runtime_config, prepare_seed, remove_development_commands, validate_application
from services.tournament_data import load_tournament_data


def test_modes_use_separate_databases_and_caches_and_reject_typos(monkeypatch):
    monkeypatch.setenv("BOT_MODE", "development")
    development = runtime_config()
    monkeypatch.setenv("BOT_MODE", "production")
    production = runtime_config()
    assert development.database_path != production.database_path
    assert development.cache_path != production.cache_path
    assert production.database_path.parent.name == "production"
    monkeypatch.setenv("BOT_MODE", "prodution")
    with pytest.raises(ValueError, match="BOT_MODE"):
        runtime_config()


def test_production_seed_retains_metadata_but_no_example_games(monkeypatch):
    seed = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json")
    monkeypatch.setenv("BOT_MODE", "production")
    clean = prepare_seed(seed)
    assert clean.matches == [] and clean.standings == []
    assert clean.teams == seed.teams and clean.stages == seed.stages
    assert seed.matches
    monkeypatch.setenv("BOT_MODE", "development")
    assert prepare_seed(seed) is seed


def test_production_checks_application_identity_before_sync_or_posts(monkeypatch):
    monkeypatch.setenv("BOT_MODE", "production")
    monkeypatch.delenv("PRODUCTION_APPLICATION_ID", raising=False)
    with pytest.raises(RuntimeError, match="PRODUCTION_APPLICATION_ID"):
        validate_application(123)
    monkeypatch.setenv("PRODUCTION_APPLICATION_ID", "456")
    with pytest.raises(RuntimeError):
        validate_application(123)
    validate_application(456)


def test_production_filters_developer_commands_without_removing_real_commands(monkeypatch):
    async def scenario():
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        async def callback(interaction):
            pass
        for name in ("test-reminder", "simulate-reminder", "testteam", "teststandings", "team", "votehistory"):
            tree.add_command(discord.app_commands.Command(name=name, description="Test", callback=callback))
        monkeypatch.setenv("BOT_MODE", "development")
        remove_development_commands(tree)
        assert len(tree.get_commands()) == 6
        monkeypatch.setenv("BOT_MODE", "production")
        remove_development_commands(tree)
        assert {command.name for command in tree.get_commands()} == {"team", "votehistory"}
        await client.close()
    asyncio.run(scenario())


def test_production_setup_has_no_sample_controls_and_simulation_entrypoints_are_disabled(monkeypatch):
    from services import server_setup
    from services.server_settings import ServerSettings
    from services.presentation import message_payload
    from services.reminder_test import run_reminder_test
    from services.prediction_test import start_prediction_test
    monkeypatch.setenv("BOT_MODE", "production")
    monkeypatch.setattr(server_setup, "get_server_settings", lambda *args: ServerSettings(123))
    async def scenario():
        view = server_setup.ServerSetupView(123)
        text = server_setup.build_server_setup_content(123)
        assert "Test Posts" not in text and "preview" not in text.lower()
        assert all("test" not in (item.custom_id + (getattr(item, "label", "") or "")).lower()
                   and "preview" not in (getattr(item, "label", "") or "").lower() for item in view.children)
        rendered = message_payload(text, controls=view)["view"]
        assert sum(isinstance(item, discord.ui.ActionRow) for item in rendered.walk_children()) == 5
        assert view.publish_daily in view.children and view.delete_daily in view.children
        assert "disabled" in await run_reminder_test(None, None, 123)
        assert "disabled" in await start_prediction_test(None, 123)
    asyncio.run(scenario())


def test_backup_contains_votes_without_changing_source(tmp_path):
    from deploy.backup_database import backup_database
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE votes (team TEXT)")
        connection.execute("INSERT INTO votes VALUES ('C9')")
    backup = backup_database(source, tmp_path / "backups")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT team FROM votes").fetchall() == [("C9",)]
    assert source.exists()
