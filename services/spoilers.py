"""One result/participant visibility policy, independent of Discord rendering.

Operational IDs and raw results remain intact: callers must use this policy at
the presentation boundary, never mutate matches used for votes or scoring.
"""

from datetime import UTC, datetime, timedelta
from contextlib import closing
import json
import re

from models import MatchStatus


RESULT_WINDOW = timedelta(hours=14)
CONNECTED_LEAD = timedelta(hours=2)
_current = None


def current_policy():
    return _current


def set_policy(policy):
    global _current
    _current = policy


def team_token(team_id, match_id=None, slot=None, *, neutral=False):
    if _current is None:
        return team_id
    return f"⟦{'choice' if neutral else 'team'}:{match_id or '-'}:{slot or 0}:{team_id}⟧"


def matchup(match):
    match_id = getattr(match, "id", getattr(match, "match_id", None))
    return f"{team_token(match.team1_id, match_id, 1)} vs {team_token(match.team2_id, match_id, 2)}"


def score_token(match):
    if _current is None:
        return None
    return f"⟦score:{match.id}⟧"


def result_token(match, text):
    """For correctness/winner text whose visibility follows one result."""
    if _current is None:
        return f"||{text}||"
    return f"⟦result:{match.id}⟧{text}⟦/result⟧"


def aggregate_token(text, match_ids=None):
    if _current is None:
        return text
    if match_ids is not None and not match_ids:
        return text  # An explicitly empty dependency set cannot reveal a result.
    if not _current.data.matches:
        return f"⟦aggregate:unknown⟧{text}⟦/aggregate⟧"
    ids = match_ids if match_ids is not None else [m.id for m in _current.data.matches
                                                  if (m.status in {MatchStatus.LIVE, MatchStatus.COMPLETED}
                                                      or m.status == MatchStatus.SCHEDULED and m.start_time <= _current.clock())
                                                  and _current.protected(m)]
    return f"⟦aggregate:{','.join(ids)}⟧{text}⟦/aggregate⟧" if ids else text


def has_tokens(text):
    return "⟦" in text


def _conceal(text):
    # One complete line per spoiler, with nested tags removed. No winner
    # underlining/emphasis or exposed table ordering can escape a hidden block.
    text = text.replace("||", "")
    # A spoiler bar's length can identify a known username. Do not preserve
    # result-based row order even when all the row text is behind spoilers.
    blocks = re.split(r"(?m)(?=^### `\d+\.` )", text)
    if len(blocks) > 2:
        prefix, rows = blocks[0], blocks[1:]
        rows.sort(key=lambda row: re.sub(r"^### `\d+\.` ", "", row).split(":", 1)[0].casefold())
        text = prefix + "".join(rows)
    return "\n".join(f"||{line}||" if line.strip() else line for line in text.splitlines())


def render_text(text, policy=None, *, neutral=False):
    policy = policy or _current
    if policy is None:
        return text
    from services.team_emojis import team_emoji, custom_emoji

    def team_replace(found, force_neutral=False):
        match_id, slot, team_id = found.groups()
        match = policy.data.matches_by_id.get(match_id)
        slot = int(slot)
        expected = (match.team1_id if slot == 1 else match.team2_id) if match and slot else team_id
        hidden = bool(match_id != "-" and (match is None or expected != team_id
                                          or slot and policy.participant_protected(match, slot)))
        if neutral or force_neutral:
            if hidden:
                placeholder = padded_name("?", policy.width)
                return placeholder if neutral else f"{custom_emoji('beeQuestion') or '❓'} `{placeholder}`"
            name = policy.data.teams_by_id[team_id].short_name if team_id in policy.data.teams_by_id else team_id
            return padded_name(name, policy.width) if neutral else policy.team(team_id, emoji=team_emoji(team_id))
        return policy.team(team_id, match=match, slot=slot, emoji=team_emoji(team_id))

    # Match IDs may contain colons (for example prediction-test:<uuid>:0).
    # Parse the slot and team from the right instead of truncating the ID.
    text = re.sub(r"⟦team:([^⟧]+):([012]):([^:⟧]+)⟧", team_replace, text)
    text = re.sub(r"⟦choice:([^⟧]+):([012]):([^:⟧]+)⟧", lambda found: team_replace(found, True), text)

    def score_replace(found):
        match = policy.data.matches_by_id.get(found[1])
        if match is None:
            return "Result unavailable"
        score = policy.score(match)
        if score:
            return score
        if match.status == MatchStatus.COMPLETED and match.winner_id:
            winner = policy.team(match.winner_id, emoji=team_emoji(match.winner_id))
            text = f"Winner: {winner}"
            return _conceal(text) if policy.protected(match) else text
        return "Score hidden while the series is live"

    text = re.sub(r"⟦score:([^⟧]+)⟧", score_replace, text)

    def result_replace(found):
        match = policy.data.matches_by_id.get(found[1])
        hidden = match is None or policy.protected(match)
        return ("Result hidden (spoiler)" if neutral else _conceal(found[2])) if hidden else found[2].replace("||", "")

    text = re.sub(r"⟦result:([^⟧]+)⟧(.*?)⟦/result⟧", result_replace, text, flags=re.S)

    def aggregate_replace(found):
        matches = (policy.data.matches if found[1] == "unknown" and policy.data.matches else
                   [policy.data.matches_by_id.get(key) for key in found[1].split(",") if key])
        if any(match is None for match in matches):
            return "Stats are hidden while live data is unavailable."
        if any(match and (match.status == MatchStatus.LIVE or match.status == MatchStatus.SCHEDULED
                          and match.start_time <= policy.clock()) for match in matches):
            return "Stats are hidden while a series is live."
        hidden = any(match is None or policy.protected(match) for match in matches)
        return ("Stats hidden (spoiler)" if neutral else _conceal(found[2])) if hidden else found[2].replace("||", "")

    return re.sub(r"⟦aggregate:([^⟧]*)⟧(.*?)⟦/aggregate⟧", aggregate_replace, text, flags=re.S)


class SpoilerLedger:
    """Persist observation times and slot provenance alongside the voting DB."""
    def __init__(self, connect):
        self.connect = connect
        with closing(connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS spoiler_ends (
                    tournament TEXT, match_id TEXT, ended_at TEXT,
                    PRIMARY KEY(tournament, match_id));
                CREATE TABLE IF NOT EXISTS spoiler_slots (
                    tournament TEXT, match_id TEXT, slot INTEGER, team_id TEXT,
                    known_at TEXT, dependencies TEXT,
                    PRIMARY KEY(tournament, match_id, slot));
            """)

    def observe(self, data, now=None):
        now = now or datetime.now(UTC)
        tournament = data.tournament.id
        with closing(self.connect()) as db, db:
            ends = {key: datetime.fromisoformat(stamp) if stamp else None for key, stamp in db.execute(
                "SELECT match_id, ended_at FROM spoiler_ends WHERE tournament=?", (tournament,))}
            for match in data.matches:
                if match.status in {MatchStatus.LIVE, MatchStatus.SCHEDULED} and ends.get(match.id) is not None:
                    ends[match.id] = None
                    db.execute("UPDATE spoiler_ends SET ended_at=NULL WHERE tournament=? AND match_id=?", (tournament, match.id))
                if match.status == MatchStatus.COMPLETED and ends.get(match.id) is None:
                    # Existing first-observation timestamps are safe to reuse.
                    row = db.execute("SELECT MIN(result_observed_at) FROM match_polls WHERE tournament_id=? AND match_id=?",
                                     (tournament, match.id)).fetchone()
                    ended = datetime.fromisoformat(row[0]) if match.id not in ends and row and row[0] else now
                    ends[match.id] = ended
                    db.execute("INSERT OR REPLACE INTO spoiler_ends VALUES (?, ?, ?)", (tournament, match.id, ended.isoformat()))
            dependencies = {}
            for match in data.matches:
                for slot, team_id in enumerate((match.team1_id, match.team2_id), 1):
                    row = db.execute("SELECT team_id, dependencies FROM spoiler_slots WHERE tournament=? AND match_id=? AND slot=?",
                                     (tournament, match.id, slot)).fetchone()
                    if row and row[0] == team_id:
                        deps = json.loads(row[1])
                    else:
                        # A newly disclosed slot can expose its team's most
                        # recent prerequisite. Slots known before completion
                        # retain their independent identity across refreshes.
                        prior = max((earlier for earlier in data.matches
                                     if earlier.start_time < match.start_time
                                     and team_id != "TBD" and team_id in {earlier.team1_id, earlier.team2_id}),
                                    key=lambda earlier: earlier.start_time, default=None)
                        deps = [prior.id] if prior and prior.status == MatchStatus.COMPLETED else []
                        db.execute("INSERT OR REPLACE INTO spoiler_slots VALUES (?, ?, ?, ?, ?, ?)",
                                   (tournament, match.id, slot, team_id, now.isoformat(), json.dumps(deps)))
                    dependencies[(match.id, slot)] = deps
        policy = SpoilerPolicy(data, ended_at=ends, dependencies=dependencies)
        set_policy(policy)
        return policy


def padded_name(name, width):
    """NBSP centering; an odd remainder belongs on the right."""
    remaining = max(0, width - len(name))
    left = remaining // 2
    return "\N{NO-BREAK SPACE}" * left + name + "\N{NO-BREAK SPACE}" * (remaining - left)


class SpoilerPolicy:
    def __init__(self, data, *, ended_at=None, dependencies=None, clock=None):
        self.data = data
        self.ended_at = ended_at or {}
        # (fixture ID, slot index 1/2) -> prerequisite series IDs. Store each
        # slot separately: an opponent being unknown never conceals its peer.
        self.dependencies = dependencies or {}
        self.clock = clock or (lambda: datetime.now(UTC))
        # Reserve a space on each side even for the longest abbreviation.
        # Otherwise a 3-character name in a 4-character field is flush left.
        self.width = max([4, *(len(team.short_name) for team in data.teams)]) + 2

    def release_at(self, match):
        if match.status != MatchStatus.COMPLETED:
            return None
        end = self.ended_at.get(match.id)
        if end is None:
            return None
        connected = []
        teams = {match.team1_id, match.team2_id} - {"TBD"}
        for future in self.data.matches:
            if future.id == match.id or future.status == MatchStatus.CANCELLED:
                continue
            if future.start_time <= match.start_time:
                continue
            depends = any(match.id in self.dependencies.get((future.id, slot), ()) for slot in (1, 2))
            if teams.intersection({future.team1_id, future.team2_id}) or depends:
                connected.append(future.start_time - CONNECTED_LEAD)
        return min([end + RESULT_WINDOW, *connected])

    def protected(self, match, now=None):
        if match.status != MatchStatus.COMPLETED:
            return True
        deadline = self.release_at(match)
        return deadline is None or (now or self.clock()) < deadline

    def participant_protected(self, match, slot, now=None):
        for prerequisite in self.dependencies.get((match.id, slot), ()):
            earlier = self.data.matches_by_id.get(prerequisite)
            if earlier is None or self.protected(earlier, now):
                return True
        return False

    def team(self, team_id, *, match=None, slot=None, emoji=None, now=None):
        team = self.data.teams_by_id.get(team_id)
        name = team.short_name if team else team_id
        text = (f"{emoji} " if emoji else "") + f"`{padded_name(name, self.width)}`"
        if match is not None and slot is not None and self.participant_protected(match, slot, now):
            return f"||{text}||"
        return text

    def score(self, match, now=None):
        # Ongoing game/series scores are never emitted, even behind spoilers.
        if match.status != MatchStatus.COMPLETED:
            return None
        if match.team1_score is None or match.team2_score is None:
            return None
        score = f"{match.team1_score}–{match.team2_score}"
        return f"||{score}||" if self.protected(match, now) else score
