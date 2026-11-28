from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from tennis_predictor.ml.player_features import evaluation as e
from tennis_predictor.ml.player_features.engine import Features, Observation, metric_pairs, windows
from tennis_predictor.ml.player_features.projection import facts
from tennis_predictor.ml.ratings.inputs import Match

DAY = date(2023, 5, 1)


def test_windows_keep_missing_matches_and_use_ratio_of_sums():
    data = [
        Observation("a", DAY - timedelta(4), "1", {"first_serve_won": (1, 2)}),
        Observation("b", DAY - timedelta(3), "2", {}),
        Observation("c", DAY - timedelta(2), "3", {"first_serve_won": (9, 10)}),
        Observation("d", DAY, "4", {"first_serve_won": (0, 100)}),
        Observation("e", DAY + timedelta(1), "5", {"first_serve_won": (0, 100)}),
    ]
    estim = windows(data, DAY)
    assert estim[5]["matches"] == 3
    assert estim[5]["span_days"] == 2
    assert estim[5]["metrics"]["first_serve_won"] == pytest.approx(
        {
            "pooled": 10 / 12,
            "mean": 0.7,
            "stddev": 0.2,
            "valid": 2,
            "denominator": 12,
            "missing": 1,
        }
    )
    assert estim == windows(data[:3], DAY)
    seen = []
    engine = Features(lambda player, day, cutoff: seen.append(player) or data)
    assert engine.get("revision1", "a", DAY) == engine.get("revision1", "a", DAY)
    assert seen == ["a"]
    data[0] = Observation("a", DAY - timedelta(4), "corrected", {"first_serve_won": (0, 2)})
    assert engine.get("revision1", "a", DAY) == estim
    assert engine.get("revision2", "a", DAY) != estim


def test_ratio_values_do_not_fabricate_missing_denominators():
    assert metric_pairs({"aces": 5, "winners": 7}, {}) == {"winners": (7, 1)}
    own = {
        "first_serve_points_won": 12,
        "first_serve_points_all": 15,
        "second_serve_points_won": 5,
        "second_serve_points_all": 9,
    }
    pair = metric_pairs(own, own)
    assert pair["first_serve_in"] == (15, 24)
    assert pair["points"] == (24, 48)
    assert pair["first_return_won"] == (3, 15)
    assert pair["second_return_won"] == (4, 9)


def test_projected_statistics_use_canonical_quality_and_paired_sides():
    received = "2023-05-01T18:00:00Z"
    rated = Match("m", DAY, "M", "tour", "hard", "a", "b", 1, True, "revision")
    result = {
        "canonical_match_id": "m",
        "nbbet_match_id": 1,
        "status": 3,
        "sets_json": '[{"score_team1":6,"score_team2":4},{"score_team1":6,"score_team2":4}]',
        "fetched_at": received,
    }
    statistics = {
        "canonical_match_id": "m",
        "scope": 0,
        "fetched_at": received,
        "service_games_won_p1": 6,
        "service_games_all_p1": 5,
        "first_serve_points_won_p1": 3,
        "first_serve_points_all_p1": 4,
    }
    ((_, (left, right)),) = facts([rated], [result], [statistics], datetime(2024, 1, 1, tzinfo=UTC))
    assert left.values["sets"] == (2, 2)
    assert left.values["first_serve_won"] == (3, 4)
    assert right.values["first_return_won"] == (1, 4)
    assert "hold" not in left.values and "break" not in right.values


def test_e04_pipeline_common_targets_symmetry_and_cache(tmp_path, monkeypatch):
    actual = e.fit_cat

    def fast(train, y, config):
        return actual(train, y, {**config, "iterations": 5})

    monkeypatch.setattr(e, "fit_cat", fast)
    matches = [
        Match(
            f"{year}-{i}",
            date(year, 1, 1) + timedelta(days=i * 5),
            "M",
            "tour",
            "hard",
            f"p{i % 6}",
            f"p{(i + 2) % 6}",
            int(i % 3 == 1),
            True,
            str(i),
        )
        for year in range(2021, 2026)
        for i in range(64)
    ]
    projected = [
        (
            m,
            (
                Observation(m.match_id, m.day, m.revision, {"first_serve_won": (i % 4, 4)}),
                Observation(m.match_id, m.day, m.revision, {"first_serve_won": (1, 4)}),
            ),
        )
        for i, m in enumerate(matches)
    ]
    vectors = e.historical_vectors(projected, {m.match_id for m in matches})
    form, _ = e.matrix([("", m.match_id) for m in matches], vectors, "10")
    assert np.allclose(form[:, 0], -form[:, 1], equal_nan=True)
    report = e.evaluate(matches, projected, {"version": "synthetic"}, tmp_path)
    assert report["targets"] == 128
    assert not report["holdout_opened"]
    cached = e.evaluate(matches, iter(()), {"version": "synthetic"}, tmp_path)
    assert cached["cache_version"] == report["cache_version"]
    assert cached["models"] == report["models"]
    with pytest.raises(ValueError, match="holdout"):
        e.evaluate(
            [Match("x", date(2026, 1, 1), "M", "tour", "hard", "a", "b", 1, True, "r")],
            [],
            {},
            tmp_path,
        )
