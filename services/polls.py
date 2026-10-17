"""Persistent private predictions, publication provenance, and confirmed results."""

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from models import MatchStatus
from services.user_settings import get_connection


@dataclass(frozen=True)
class Poll:
    guild_id: int
    match_id: str
    tournament_id: str
    team1_id: str
    team2_id: str
    start_time: datetime
    closed: bool
    winner_id: str | None


def initialize_polls():
    with closing(get_connection()) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS match_polls (
                guild_id INTEGER NOT NULL,
                match_id TEXT NOT NULL,
                tournament_id TEXT NOT NULL,
                team1_id TEXT NOT NULL,
                team2_id TEXT NOT NULL,
                start_time TEXT NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0,
                winner_id TEXT,
                PRIMARY KEY (guild_id, match_id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS poll_votes (
                guild_id INTEGER NOT NULL,
                match_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                team_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, match_id, user_id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS poll_messages (
                guild_id INTEGER NOT NULL, match_id TEXT NOT NULL,
                channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                closed_display INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, match_id)
            )
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(poll_votes)")}
        if "display_name" not in columns:
            connection.execute("ALTER TABLE poll_votes ADD COLUMN display_name TEXT")
        poll_columns = {row[1] for row in connection.execute("PRAGMA table_info(match_polls)")}
        if "result_observed_at" not in poll_columns:
            connection.execute("ALTER TABLE match_polls ADD COLUMN result_observed_at TEXT")
        legacy_publication = "published_at" not in poll_columns
        if legacy_publication:
            connection.execute("ALTER TABLE match_polls ADD COLUMN published_at TEXT")
        for name in ("created_at", "vote_team1_id", "vote_team2_id"):
            if name not in columns:
                connection.execute(f"ALTER TABLE poll_votes ADD COLUMN {name} TEXT")
        connection.execute("UPDATE poll_votes SET created_at = updated_at WHERE created_at IS NULL")
        connection.execute("""
            UPDATE poll_votes SET
                vote_team1_id = (SELECT team1_id FROM match_polls WHERE match_polls.guild_id = poll_votes.guild_id AND match_polls.match_id = poll_votes.match_id),
                vote_team2_id = (SELECT team2_id FROM match_polls WHERE match_polls.guild_id = poll_votes.guild_id AND match_polls.match_id = poll_votes.match_id)
            WHERE vote_team1_id IS NULL OR vote_team2_id IS NULL
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS daily_prediction_posts (
                guild_id INTEGER NOT NULL, tournament_id TEXT NOT NULL, match_day TEXT NOT NULL,
                channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, signature TEXT NOT NULL,
                PRIMARY KEY (guild_id, tournament_id, match_day)
            )
        """)
        notices_exist = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='voting_open_notices'").fetchone()
        connection.execute("""
            CREATE TABLE IF NOT EXISTS voting_open_notices (
                guild_id INTEGER NOT NULL, tournament_id TEXT NOT NULL, match_id TEXT NOT NULL,
                team1_id TEXT NOT NULL, team2_id TEXT NOT NULL,
                PRIMARY KEY (guild_id, tournament_id, match_id, team1_id, team2_id)
            )
        """)
        if not notices_exist:
            # Upgrades must not replay notifications for already-published polls.
            connection.execute("""INSERT OR IGNORE INTO voting_open_notices
                SELECT guild_id, tournament_id, match_id, team1_id, team2_id
                FROM match_polls WHERE published_at IS NOT NULL""")
        # Existing votes prove an opportunity existed; do not guess publication
        # times for legacy polls that never received a vote.
        if legacy_publication:
            connection.execute("""
            UPDATE match_polls SET published_at = (
                SELECT MIN(created_at) FROM poll_votes
                WHERE poll_votes.guild_id = match_polls.guild_id AND poll_votes.match_id = match_polls.match_id
            ) WHERE published_at IS NULL AND EXISTS (
                SELECT 1 FROM poll_votes WHERE poll_votes.guild_id = match_polls.guild_id AND poll_votes.match_id = match_polls.match_id
            )
            """)


def voting_notice_pairs(guild_id, tournament_id):
    with closing(get_connection()) as connection:
        return set(connection.execute("SELECT match_id, team1_id, team2_id FROM voting_open_notices WHERE guild_id=? AND tournament_id=?",
                                      (guild_id, tournament_id)).fetchall())


def mark_voting_notices(guild_id, tournament_id, pairs):
    with closing(get_connection()) as connection, connection:
        connection.executemany("INSERT OR IGNORE INTO voting_open_notices VALUES (?, ?, ?, ?, ?)",
                               [(guild_id, tournament_id, *pair) for pair in pairs])


def create_poll(guild_id, match):
    if match.status != MatchStatus.SCHEDULED or "TBD" in {match.team1_id, match.team2_id}:
        raise ValueError("A poll needs a scheduled match with two known teams.")
    with closing(get_connection()) as connection, connection:
        connection.execute("""
            INSERT INTO match_polls (guild_id, match_id, tournament_id, team1_id, team2_id, start_time)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, match_id) DO NOTHING
        """, (guild_id, match.id, match.tournament_id, match.team1_id, match.team2_id,
              match.start_time.astimezone(UTC).isoformat()))


def _poll(row):
    return Poll(*row[:5], datetime.fromisoformat(row[5]), bool(row[6]), row[7])


def get_polls(guild_id, tournament_id):
    with closing(get_connection()) as connection:
        rows = connection.execute("""
            SELECT guild_id, match_id, tournament_id, team1_id, team2_id, start_time, closed, winner_id
            FROM match_polls WHERE guild_id = ? AND tournament_id = ?
        """, (guild_id, tournament_id)).fetchall()
    return [_poll(row) for row in rows]


def cast_vote(guild_id, match_id, user_id, team_id, now, display_name=None, expected_pair=None):
    if now.tzinfo is None:
        raise ValueError("Vote time must be timezone-aware.")
    with closing(get_connection()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("""
            SELECT start_time, closed, team1_id, team2_id FROM match_polls
            WHERE guild_id = ? AND match_id = ?
        """, (guild_id, match_id)).fetchone()
        if row is None or row[1] or now >= datetime.fromisoformat(row[0]) - timedelta(minutes=5):
            raise ValueError("Voting has closed for this match.")
        if team_id not in row[2:]:
            raise ValueError("Choose one of the two teams in this poll.")
        if expected_pair is not None and set(row[2:]) != set(expected_pair):
            raise ValueError("This matchup changed. Please use the current prediction buttons.")
        previous = connection.execute(
            "SELECT team_id FROM poll_votes WHERE guild_id = ? AND match_id = ? AND user_id = ?",
            (guild_id, match_id, user_id),
        ).fetchone()
        connection.execute("""
            INSERT INTO poll_votes (guild_id, match_id, user_id, team_id, updated_at, display_name, created_at, vote_team1_id, vote_team2_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, match_id, user_id)
            DO UPDATE SET team_id = excluded.team_id, updated_at = excluded.updated_at, display_name = excluded.display_name,
                          vote_team1_id = excluded.vote_team1_id, vote_team2_id = excluded.vote_team2_id
        """, (guild_id, match_id, user_id, team_id, now.astimezone(UTC).isoformat(), display_name,
              now.astimezone(UTC).isoformat(), row[2], row[3]))
        return previous is not None and previous[0] != team_id


def reconcile_polls(guild_id, data, now, *, fresh=True):
    matches = data.matches_by_id
    with closing(get_connection()) as connection, connection:
        for poll in get_polls(guild_id, data.tournament.id):
            match = matches.get(poll.match_id) if fresh else None
            start = match.start_time if match else poll.start_time
            closed = poll.closed or now >= start - timedelta(minutes=5)
            winner = poll.winner_id
            team1, team2 = poll.team1_id, poll.team2_id
            if fresh:
                if match is None or match.status != MatchStatus.SCHEDULED:
                    closed = True
                if match:
                    team1, team2 = match.team1_id, match.team2_id
                    if "TBD" in {team1, team2}:
                        closed = True
                winner = match.winner_id if match and match.status == MatchStatus.COMPLETED and match.winner_id in {team1, team2} else None
                if winner:
                    connection.execute("""
                        UPDATE match_polls SET result_observed_at = COALESCE(result_observed_at, ?)
                        WHERE guild_id = ? AND match_id = ? AND result_observed_at IS NULL
                    """, (now.astimezone(UTC).isoformat(), guild_id, poll.match_id))
                elif match and match.status != MatchStatus.COMPLETED:
                    connection.execute("""
                        UPDATE match_polls SET result_observed_at = NULL
                        WHERE guild_id = ? AND match_id = ? AND result_observed_at IS NOT NULL
                    """, (guild_id, poll.match_id))
            if (start, closed, winner, team1, team2) == (poll.start_time, poll.closed, poll.winner_id, poll.team1_id, poll.team2_id):
                continue
            if {team1, team2} != {poll.team1_id, poll.team2_id}:
                connection.execute("UPDATE match_polls SET published_at = NULL WHERE guild_id = ? AND match_id = ?",
                                   (guild_id, poll.match_id))
            connection.execute("""
                UPDATE match_polls SET start_time = ?, closed = ?, winner_id = ?, team1_id = ?, team2_id = ?
                WHERE guild_id = ? AND match_id = ?
            """, (start.astimezone(UTC).isoformat(), int(closed), winner, team1, team2, guild_id, poll.match_id))


def get_leaderboard(guild_id, tournament_id):
    with closing(get_connection()) as connection:
        rows = connection.execute("""
            SELECT votes.user_id,
                   SUM(CASE WHEN polls.winner_id = votes.team_id THEN 1 ELSE 0 END) AS points,
                   COUNT(*) AS votes_cast
            FROM poll_votes AS votes
            JOIN match_polls AS polls USING (guild_id, match_id)
            WHERE polls.guild_id = ? AND polls.tournament_id = ? AND polls.closed = 1
            GROUP BY votes.user_id ORDER BY points DESC, votes.user_id
        """, (guild_id, tournament_id)).fetchall()
    return rows


def get_user_votes(guild_id, tournament_id, user_id, *, include_open=False, now=None):
    """Only the requesting user's current picks, including still-private votes."""
    with closing(get_connection()) as connection:
        rows = connection.execute("""
            SELECT polls.guild_id, polls.match_id, polls.tournament_id,
                   polls.team1_id, polls.team2_id, polls.start_time,
                   polls.closed, polls.winner_id, votes.team_id, votes.vote_team1_id, votes.vote_team2_id
            FROM poll_votes AS votes
            JOIN match_polls AS polls USING (guild_id, match_id)
            WHERE polls.guild_id = ? AND polls.tournament_id = ? AND votes.user_id = ?
            ORDER BY polls.start_time, polls.match_id
        """, (guild_id, tournament_id, user_id)).fetchall()
    result = []
    for row in rows:
        poll = _poll(row[:8])
        if set(row[9:11]) != {poll.team1_id, poll.team2_id}:
            poll = replace(poll, team1_id=row[9], team2_id=row[10], closed=True, winner_id=None)
        result.append((poll, row[8]))
    if include_open:
        now = now or datetime.now(UTC)
        saved_ids = {poll.match_id for poll, _ in result}
        published = get_published_match_ids(guild_id, tournament_id)
        result.extend((poll, None) for poll in get_polls(guild_id, tournament_id)
                      if poll.match_id not in saved_ids and poll.match_id in published
                      and not poll.closed and now < poll.start_time - timedelta(minutes=5))
        result.sort(key=lambda row: (row[0].start_time, row[0].match_id))
    return result


def get_latest_vote_match_id(guild_id, tournament_id, user_id):
    with closing(get_connection()) as connection:
        row = connection.execute("""
            SELECT votes.match_id FROM poll_votes AS votes
            JOIN match_polls AS polls USING (guild_id, match_id)
            WHERE votes.guild_id = ? AND polls.tournament_id = ? AND votes.user_id = ?
            ORDER BY votes.updated_at DESC, votes.match_id LIMIT 1
        """, (guild_id, tournament_id, user_id)).fetchone()
    return row[0] if row else None


def get_poll_message(guild_id, match_id):
    with closing(get_connection()) as connection:
        return connection.execute(
            "SELECT channel_id, message_id, closed_display FROM poll_messages WHERE guild_id = ? AND match_id = ?",
            (guild_id, match_id),
        ).fetchone()


def record_poll_message(guild_id, match_id, channel_id, message_id):
    with closing(get_connection()) as connection, connection:
        connection.execute("""
            INSERT INTO poll_messages (guild_id, match_id, channel_id, message_id) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, match_id) DO NOTHING
        """, (guild_id, match_id, channel_id, message_id))


def mark_poll_display_closed(guild_id, match_id):
    with closing(get_connection()) as connection, connection:
        connection.execute("UPDATE poll_messages SET closed_display = 1 WHERE guild_id = ? AND match_id = ?",
                           (guild_id, match_id))


def get_daily_post(guild_id, tournament_id, day):
    with closing(get_connection()) as connection:
        return connection.execute("""
            SELECT channel_id, message_id, signature FROM daily_prediction_posts
            WHERE guild_id = ? AND tournament_id = ? AND match_day = ?
        """, (guild_id, tournament_id, day)).fetchone()


def get_daily_post_days(guild_id, tournament_id):
    with closing(get_connection()) as connection:
        return {row[0] for row in connection.execute(
            "SELECT match_day FROM daily_prediction_posts WHERE guild_id = ? AND tournament_id = ?",
            (guild_id, tournament_id))}


def save_daily_post(guild_id, tournament_id, day, channel_id, message_id, signature):
    with closing(get_connection()) as connection, connection:
        connection.execute("""
            INSERT INTO daily_prediction_posts VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, tournament_id, match_day) DO UPDATE SET
                channel_id = excluded.channel_id, message_id = excluded.message_id, signature = excluded.signature
        """, (guild_id, tournament_id, day, channel_id, message_id, signature))


def get_poll_results(guild_id, match_id):
    with closing(get_connection()) as connection:
        row = connection.execute("SELECT closed, team1_id, team2_id FROM match_polls WHERE guild_id = ? AND match_id = ?",
                                 (guild_id, match_id)).fetchone()
        if row is None or not row[0]:
            raise ValueError("Prediction results remain hidden until voting closes.")
        return connection.execute("""
            SELECT user_id, team_id, display_name FROM poll_votes
            WHERE guild_id = ? AND match_id = ? AND team_id IN (?, ?)
              AND ((vote_team1_id = ? AND vote_team2_id = ?) OR (vote_team1_id = ? AND vote_team2_id = ?))
            ORDER BY user_id
        """, (guild_id, match_id, row[1], row[2], row[1], row[2], row[2], row[1])).fetchall()


def get_upcoming_match_votes(guild_id, match_id):
    """Public results are available only after the real reminder was sent."""
    from services.delivery_records import get_delivery_record
    if get_delivery_record(guild_id, match_id) is None:
        raise ValueError("Votes are private until voting closes, 5 minutes before the match.")
    return get_poll_results(guild_id, match_id)


def has_match_votes(guild_id, match_id):
    with closing(get_connection()) as connection:
        return connection.execute(
            "SELECT 1 FROM poll_votes WHERE guild_id = ? AND match_id = ? LIMIT 1",
            (guild_id, match_id),
        ).fetchone() is not None


def record_poll_opportunity(guild_id, match_id, team1_id, team2_id, published_at):
    """Only an acknowledged Discord post/edit before cutoff makes a poll eligible."""
    with closing(get_connection()) as connection, connection:
        row = connection.execute("SELECT start_time, team1_id, team2_id FROM match_polls WHERE guild_id = ? AND match_id = ?",
                                 (guild_id, match_id)).fetchone()
        if row is None or (row[1], row[2]) != (team1_id, team2_id):
            return
        if published_at >= datetime.fromisoformat(row[0]) - timedelta(minutes=5):
            return
        connection.execute("UPDATE match_polls SET published_at = COALESCE(published_at, ?) WHERE guild_id = ? AND match_id = ?",
                           (published_at.astimezone(UTC).isoformat(), guild_id, match_id))


def get_published_match_ids(guild_id, tournament_id):
    with closing(get_connection()) as connection:
        return {row[0] for row in connection.execute(
            "SELECT match_id FROM match_polls WHERE guild_id = ? AND tournament_id = ? AND published_at IS NOT NULL",
            (guild_id, tournament_id))}


def get_recent_completed_match_ids(guild_id, tournament_id, now):
    with closing(get_connection()) as connection:
        rows = connection.execute("""
            SELECT match_id, result_observed_at FROM match_polls
            WHERE guild_id = ? AND tournament_id = ? AND result_observed_at IS NOT NULL
        """, (guild_id, tournament_id)).fetchall()
    return {match_id for match_id, observed in rows
            if timedelta(0) <= now - datetime.fromisoformat(observed) < timedelta(hours=1)}
