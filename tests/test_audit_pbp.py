"""Pure structural checks; no DB or S3 credentials required."""
import pandas as pd

from tennis_predictor.ingestion.audit_pbp import inspect_match


def frame(**overrides):
    data = {
        "set_num": [1, 1, 1, 1],
        "game_num": [1, 1, 2, 2],
        "point_num": [1, 2, 1, 2],
        "point_winner": [1, 2, 1, 1],
        "game_complete": [True, True, True, True],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_structurally_ok_does_not_claim_full_match():
    result = inspect_match(frame())
    assert result["status"] == "structurally_ok"
    assert result["games"] == 2


def test_incomplete_game_is_review_not_proven_corruption():
    result = inspect_match(frame(game_complete=[True, True, False, False]))
    assert result["status"] == "review"
    assert result["incomplete_games"] == [[1, 2]]


def test_missing_point_and_unresolved_winner():
    result = inspect_match(frame(point_num=[1, 3, 1, 2], point_winner=[1, None, 1, 1]))
    assert "point_index_gap:1:1" in result["issues"]
    assert "unresolved_point_winner" in result["issues"]


def test_empty():
    result = inspect_match(frame().iloc[:0])
    assert result["status"] == "missing"
