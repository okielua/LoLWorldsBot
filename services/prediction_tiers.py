"""League-themed prediction tiers with guaranteed first-place Challenger."""

from services.prediction_stats import competition_ranks


# Merged all-region shares supplied by the server owner. Header cutoffs are
# cumulative reference percentages, not percentiles within this small server.
TIERS = (
    ('challenger', 'Challenger', '0.9'),
    ('diamond', 'Diamond', '35.9'),
    ('gold', 'Gold', '79.9'),
    ('iron', 'Iron', '100.0'),
)
LOWER_WEIGHTS = (350, 440, 197)  # 35%, 44%, 19.7%; Challenger is reserved first.


def prediction_tiers(rows):
    rows = sorted(rows, key=lambda r: (-r.correct, -r.accuracy, r.user_id))
    ranks = competition_ranks(rows)
    leaders = sum(ranks[row.user_id] == 1 for row in rows)
    remaining = len(rows) - leaders
    total = sum(LOWER_WEIGHTS)
    shares = [divmod(remaining * weight, total) for weight in LOWER_WEIGHTS]
    sizes = [whole for whole, _ in shares]
    for index in sorted(range(3), key=lambda i: (-shares[i][1], i))[:remaining - sum(sizes)]:
        sizes[index] += 1
    assignments = {}
    for row in rows:
        rank = ranks[row.user_id]
        if rank == 1:
            assignments[row.user_id] = 'challenger'
            continue
        # A tied group uses its highest position, so a cutoff never separates it.
        offset = rank - 1 - leaders
        boundary = 0
        for tier, size in zip(TIERS[1:], sizes):
            boundary += size
            if offset < boundary:
                assignments[row.user_id] = tier[0]
                break
    return assignments
