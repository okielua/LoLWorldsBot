"""Swiss series records calculated from validated live match results."""

from models import MatchStatus, Standing


def calculate_swiss_standings(data):
    stages = {stage.id for stage in data.stages if "swiss" in stage.name.lower()}
    records = {}
    for match in data.matches:
        if match.stage_id not in stages or match.status in {MatchStatus.CANCELLED, MatchStatus.POSTPONED}:
            continue
        for team_id in (match.team1_id, match.team2_id):
            if team_id != "TBD":
                records.setdefault((match.stage_id, team_id), Standing(
                    data.tournament.id, match.stage_id, team_id,
                ))
        if match.status != MatchStatus.COMPLETED or match.winner_id is None:
            continue
        for team_id in (match.team1_id, match.team2_id):
            record = records[(match.stage_id, team_id)]
            if team_id == match.winner_id:
                record.wins += 1
            else:
                record.losses += 1
    # No invented official rank or tiebreaker. A Bo3 win counts as one series win.
    return list(records.values())
