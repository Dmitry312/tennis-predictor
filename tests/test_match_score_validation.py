from infra.audit.match_score_validation import evaluate_scores


def test_score_validation_counts_mismatches_and_unknowns():
    records = [
        {"match_id": "a", "sets": [{"x": 6, "y": 4}, {"x": 6, "y": 2}], "winner": 1},
        {"match_id": "b", "sets": [{"x": 6, "y": 4}, {"x": 6, "y": 2}], "winner": 2},
        {"match_id": "c", "sets": [{"x": 6, "y": 6}], "winner": 1},
        {"match_id": "d", "sets": None, "winner": 2},
    ]
    result = evaluate_scores(records, sets_key="sets", p1_games_key="x", p2_games_key="y",
                             winner_key="winner", player1_value=1, player2_value=2)
    assert result["counts"] == {"examined": 4, "verified": 2, "mismatch": 1, "unverifiable": 2}
    assert result["mismatch_examples"] == [{"match": "b", "predicted": 1, "actual": 2}]


def test_rejects_bool_and_partial_tied_match():
    records = [{"sets": [{"x": True, "y": 3}], "winner": 1},
               {"sets": [{"x": 6, "y": 4}, {"x": 4, "y": 6}], "winner": 2}]
    result = evaluate_scores(records, sets_key="sets", p1_games_key="x", p2_games_key="y",
                             winner_key="winner", player1_value=1, player2_value=2)
    assert result["counts"]["unverifiable"] == 2
