"""Fetch and validate the local cache without logging in to Discord."""

import asyncio
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.live_tournament import LiveTournamentStore
from services.tournament_data import load_tournament_data
from services.runtime_config import runtime_config, prepare_seed


async def main():
    store = LiveTournamentStore(
        prepare_seed(load_tournament_data(ROOT / "data" / "worlds_2026.json")),
        runtime_config().cache_path,
    )
    if not await store.refresh():
        raise SystemExit("Refresh failed; previous data retained. See the log above.")
    print(f"Cached {len(store.data.matches)} matches at {store.fetched_at.isoformat()}")
    for match in store.data.matches[:2]:
        print(match.start_time.isoformat(), match.team1_id, "vs", match.team2_id, match.status.value)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
