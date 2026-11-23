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
        "numeric_fields": [{}],
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
    }) == {"p1_rank": None, "sets_structure": None, "sets_anonymous_shape": None}


def test_sets_numeric_fields_only_no_names_or_odds():
    raw = '[{"1":6,"2":4,"3":[7,5],"player":"SECRET","30":1.55,"4":"SECRET","5":{"1":6}},{"1":0,"2":0}]'
    result = review.safe_sets(raw)
    assert result["numeric_fields"] == [{"1": 6, "2": 4, "3": [7, 5]}, {"1": 0, "2": 0}]
    assert "SECRET" not in repr(result)
    assert "1.55" not in repr(result)


def test_sets_reject_oversize_and_nested_sensitive_values():
    assert review.safe_sets("x" * 4097) is None
    assert review.safe_sets('[{"1":"PLAYER","2":[1,"PLAYER"],"3":true,"4":-1,"5":100}]')["numeric_fields"] == [{}]
    assert review.safe_sets('[{"1":1}]' * 6) is None


def test_anonymous_set_shape_keeps_scores_and_redacts_strings_and_keys():
    raw = '[{"secret_player_name":{"score":"6","other":"PRIVATE"},"games":[4,0],"odds":1.55}]'
    shape = review.safe_set_shape(raw)
    assert shape[0]["type"] == "object"
    assert "secret_player_name" not in repr(shape)
    assert "PRIVATE" not in repr(shape)
    assert "1.55" not in repr(shape)
    assert '"value": 6' in repr(shape) or "'value': 6" in repr(shape)
    assert review.safe_set_shape("x" * 4097) is None


def test_anonymous_set_shape_bounded_and_no_negative_numbers():
    shape = review.safe_set_shape('[{"x":-2,"y":true,"z":[1,2]}]')
    assert shape[0]["fields"]["K1"] == {"type": "other"}
    assert shape[0]["fields"]["K2"] == {"type": "bool", "value": True}
    assert shape[0]["fields"]["K3"]["items"][0]["value"] == 1
