"""Environment selection without sharing production votes or cached schedules."""

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(Path(os.environ.get("BOT_ENV_FILE", ROOT / ".env")))


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str
    database_path: Path
    cache_path: Path

    @property
    def development(self):
        return self.mode == "development"


def runtime_config():
    mode = os.environ.get("BOT_MODE", "development").strip().lower()
    if mode not in {"development", "production"}:
        raise ValueError("BOT_MODE must be development or production.")
    directory = ROOT / "data" if mode == "development" else ROOT / "data" / "production"
    return RuntimeConfig(mode, directory / "worldsbot.db", directory / "worlds_2026_cache.json")


def development_enabled():
    return runtime_config().development


def validate_application(application_id):
    if not development_enabled():
        expected_id = os.environ.get("PRODUCTION_APPLICATION_ID", "")
        if not expected_id.isdecimal() or application_id != int(expected_id):
            raise RuntimeError("Production requires PRODUCTION_APPLICATION_ID matching the live bot's application ID.")


def prepare_seed(data):
    # Seed metadata provides tournament dates, stages and logos. Its illustrative
    # matches and standings must never appear in production if the source fails.
    return data if development_enabled() else replace(data, matches=[], standings=[])


def remove_development_commands(tree):
    if not development_enabled():
        for command in list(tree.get_commands()):
            if command.name.startswith(("test", "simulate")):
                tree.remove_command(command.name)
