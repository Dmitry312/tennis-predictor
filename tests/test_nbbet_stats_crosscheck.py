"""Offline tests for full-field NBBet statistics reconciliation."""
import pytest
from tennis_predictor.ingestion.nbbet.stats_crosscheck import (
    FIELDS, latest_rows, stat_differences,
)


def row(scope, **overrides):
    return {"scope": scope, **{field: None for field in FIELDS}, **overrides}


def test_identical_stats_all_scopes():
    old = [row(0, aces_p1=4, first_serve_points_all_p2=22),
           row(1, first_serve_points_all_p2=12),
           row(2, first_serve_points_all_p2=10)]
    assert stat_differences(old, old) == []


def test_detects_actual_geerts_second_set_correction():
    old = [row(0, first_serve_points_all_p2=22),
           row(1, first_serve_points_all_p2=12),
           row(2, first_serve_points_all_p2=10)]
    new = [row(0, first_serve_points_all_p2=21),
           row(1, first_serve_points_all_p2=12),
           row(2, first_serve_points_all_p2=9)]
    assert stat_differences(old, new) == [
        (0, "first_serve_points_all_p2", 22.0, 21.0),
        (2, "first_serve_points_all_p2", 10.0, 9.0),
    ]


def test_checks_all_28_stat_fields():
    assert len(FIELDS) == 28
    for field in FIELDS:
        assert stat_differences([row(0, **{field: 1})],
                                [row(0, **{field: 2})]) == [(0, field, 1.0, 2.0)]


def test_rejects_source_missing_stat_and_scope():
    with pytest.raises(ValueError, match="removed"):
        stat_differences([row(0, aces_p1=2)], [row(0)])
    with pytest.raises(ValueError, match="scopes"):
        stat_differences([row(0), row(1)], [row(0)])
    with pytest.raises(ValueError, match="duplicate"):
        stat_differences([row(0)], [row(0), row(0)])


def test_newest_parquet_row_wins():
    rows = [
        {"canonical_match_id": "abc", "scope": 2, "fetched_at": "2026-11-23 12:20:20", "aces_p1": 1},
        {"canonical_match_id": "abc", "scope": 2, "fetched_at": "2026-11-24 10:20:20", "aces_p1": 2},
        {"canonical_match_id": "abc", "scope": 1, "fetched_at": "2026-11-23 12:20:20", "aces_p1": 3},
    ]
    selected = latest_rows(rows)
    assert selected[("abc", 2)][1]["aces_p1"] == 2
    assert selected[("abc", 1)][1]["aces_p1"] == 3
