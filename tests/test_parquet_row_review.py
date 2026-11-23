"""Offline safety tests for the Parquet review serialization boundary."""
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "infra/audit/parquet_row_review.py"
spec = importlib.util.spec_from_file_location("parquet_row_review", PATH)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def test_pbp_redacts_identity_and_arbitrary_strings():
    row = {
        "canonical_match_id": "SECRET_MATCH_ID",
        "player1": "SECRET_PLAYER",
        "odds_open_p1": 1.75,
        "surface": "clay",
        "set_num": 1,
        "point_winner": 2,
        "score_server": "15",
        "score_returner": "SECRET_PLAYER",
        "point_type": "SECRET_PLAYER",
        "game_complete": True,
    }
    result = review.sanitize_row("pbp/", row)
    assert result == {
        "set_num": 1, "point_winner": 2,
        "score_server": "15", "score_returner": None,
        "point_type": None, "game_complete": True,
    }
    assert "SECRET" not in repr(result)


def test_stats_mixed_types_and_null_vs_zero():
    assert review.sanitize_row("nbbet_stats/", {
        "canonical_match_id": "secret",
        "scope": 0, "aces_p1": 0, "aces_p2": None,
        "winners_p1": 3.5, "winners_p2": float("nan"),
        "service_games_all_p1": True,
    }) == {
        "scope": 0, "aces_p1": 0, "aces_p2": None,
        "winners_p1": 3.5, "winners_p2": None,
        "service_games_all_p1": None,
    }


def test_details_sets_structure_does_not_expose_player_names():
    row = {
        "canonical_match_id": "SECRET_MATCH_ID",
        "sets_json": '[{"player":"SECRET_PLAYER","p1":6,"p2":4}]',
        "players_json": '{"p1":"SECRET_PLAYER"}',
        "extra_json": "SECRET_EXTRA",
        "status": 3, "has_coverage": True,
        "p1_rank": 10.0, "p2_rank": None,
    }
    result = review.sanitize_row("nbbet_details/", row)
    assert result["sets_structure"] == {
        "kind": "list", "length": 1, "element_types": ["dict"],
    }
    assert result["p1_rank"] == 10
    assert result["p2_rank"] is None
    assert "SECRET" not in repr(result)


def test_reject_unknown_prefix_and_unsafe_surfaces():
    with pytest.raises(ValueError):
        review.sanitize_row("nbbet_raw/", {})
    assert review.surface_from_pg("hard") == "hard"
    assert review.surface_from_pg("SECRET") is None
    assert review.sanitize_row("pbp/", {"surface": "grass"}) == {}


def test_bad_scores_and_non_integral_rank_are_null():
    assert review.sanitize_row("pbp/", {
        "score_server": "40-SECRET", "score_returner": "AD",
        "point_winner": 1.5,
    }) == {"score_server": None, "score_returner": "AD", "point_winner": None}
    assert review.sanitize_row("nbbet_details/", {
        "p1_rank": 12.5, "sets_json": "not-json",
    }) == {"p1_rank": None, "sets_structure": None}
