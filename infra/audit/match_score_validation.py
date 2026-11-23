"""Offline validation of match score hypotheses against independently known winners.

This module never reads production systems. Feed it sampled records after
reviewing the exact source keys and player orientation.
"""
from collections import Counter


def evaluate_scores(records, *, sets_key, p1_games_key, p2_games_key,
                    winner_key, player1_value, player2_value):
    """Return coverage and mismatches; never infer a winner from an incomplete set."""
    totals = Counter()
    mismatches = []
    for record in records:
        totals["examined"] += 1
        sets = record.get(sets_key)
        actual = record.get(winner_key)
        if not isinstance(sets, list) or not sets or actual not in (player1_value, player2_value):
            totals["unverifiable"] += 1
            continue
        wins = [0, 0]
        valid = True
        for item in sets:
            if not isinstance(item, dict):
                valid = False
                break
            a, b = item.get(p1_games_key), item.get(p2_games_key)
            if type(a) is not int or type(b) is not int or not 0 <= a <= 99 or not 0 <= b <= 99 or a == b:
                valid = False
                break
            wins[0 if a > b else 1] += 1
        if not valid or wins[0] == wins[1]:
            totals["unverifiable"] += 1
            continue
        predicted = player1_value if wins[0] > wins[1] else player2_value
        totals["verified"] += 1
        if predicted != actual:
            totals["mismatch"] += 1
            if len(mismatches) < 30:
                mismatches.append({"match": record.get("match_id"), "predicted": predicted, "actual": actual})
    return {"counts": dict(totals), "mismatch_examples": mismatches}
