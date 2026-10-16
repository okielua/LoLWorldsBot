"""Read the same schedule endpoint as the LoL Esports website."""

import json
from datetime import UTC, datetime

import aiohttp


ENDPOINT = "https://lolesports.com/api/gql"
WORLDS_LEAGUE_ID = "98767975604431411"
WORLDS_2026_ID = "115660540725177488"
QUERY_HASH = "a4ca0434b4c3af4f3f95b4d7b03fe2a3fcdda0a36887d4f181da99cf3786f664"


class SourceError(ValueError):
    """The source did not provide a complete, usable response."""


class LoLEsportsClient:
    def __init__(self, *, client_version="4078aa9", query_hash=QUERY_HASH):
        self.client_version = client_version
        self.query_hash = query_hash

    async def fetch_events(self, start: datetime, end: datetime, *, league_ids=None):
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise SourceError("Fetch interval must be timezone-aware and increasing.")
        variables = {
            "hl": "en-GB", "sport": "lol",
            "leagues": [WORLDS_LEAGUE_ID] if league_ids is None else league_ids,
            "eventDateStart": start.astimezone(UTC).isoformat(),
            "eventDateEnd": end.astimezone(UTC).isoformat(),
            # Include live and completed matches; an upcoming-only query loses results.
            "eventState": ["unstarted", "inProgress", "completed"],
            "eventType": "match", "pageSize": 40,
        }
        headers = {
            "User-Agent": "LoLWorldsBot/1.0",
            "Accept": "application/graphql-response+json,application/json;q=0.9",
            "Content-Type": "application/json",
            "apollographql-client-name": "Esports Web",
            "apollographql-client-version": self.client_version,
        }
        extensions = {"persistedQuery": {"version": 1, "sha256Hash": self.query_hash}}
        events = []
        seen_tokens = set()
        seen_ids = set()
        async with aiohttp.ClientSession(
            headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            for _ in range(20):
                params = {
                    "operationName": "homeEvents",
                    "variables": json.dumps(variables, separators=(",", ":")),
                    "extensions": json.dumps(extensions, separators=(",", ":")),
                }
                async with session.get(ENDPOINT, params=params) as response:
                    response.raise_for_status()
                    raw = await response.json()
                if not isinstance(raw, dict) or raw.get("errors"):
                    raise SourceError("GraphQL returned errors or an invalid response.")
                try:
                    data = raw["data"]["esports"]
                    page = data["events"]
                    newer = data["pages"]["newer"]
                except (KeyError, TypeError) as error:
                    raise SourceError("Missing schedule or pagination fields.") from error
                if not isinstance(page, list) or (newer is not None and not isinstance(newer, str)):
                    raise SourceError("Invalid schedule or pagination fields.")
                for event in page:
                    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                        raise SourceError("Missing event ID.")
                    if event["id"] in seen_ids:
                        raise SourceError("Duplicate event ID across schedule pages.")
                    seen_ids.add(event["id"])
                    events.append(event)
                if newer is None:
                    return events
                if not newer or newer in seen_tokens:
                    raise SourceError("Schedule pagination did not advance.")
                seen_tokens.add(newer)
                variables["pageToken"] = newer
        raise SourceError("Schedule exceeded the pagination limit.")
