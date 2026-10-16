"""Read-only source verification. Never starts Discord or changes the cache."""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.lolesports import LoLEsportsClient


async def main():
    client = LoLEsportsClient()
    for year in (2026, 2025):
        events = await client.fetch_events(
            datetime(year, 10, 1, tzinfo=UTC), datetime(year, 12, 1, tzinfo=UTC)
        )
        print(year, "matches:", len(events), "states:", dict(Counter(e["state"] for e in events)))
        for event in events[:2]:
            print(event["id"], event["startTime"], event["state"], [
                (team["code"], team.get("result")) for team in event["matchTeams"]
            ])
    # Look for a live series; its absence is not a source failure.
    events = await client.fetch_events(
        datetime(2026, 10, 13, tzinfo=UTC), datetime(2026, 10, 16, tzinfo=UTC), league_ids=[]
    )
    live = [event for event in events if event["state"] == "inProgress"]
    print("Live matches observed:", len(live))
    for event in live[:2]:
        print(event["id"], event["state"], [
            (team["code"], team.get("result")) for team in event["matchTeams"]
        ])


if __name__ == "__main__":
    asyncio.run(main())
