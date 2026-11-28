from datetime import date, timedelta

import numpy as np
import pytest

from tennis_predictor.ml.baselines import optimization as opt
from tennis_predictor.ml.ratings.inputs import Match


def test_signed_woe_symmetric_finite_and_unknown():
    pairs = np.column_stack((np.arange(-200, 200), np.arange(200, -200, -1)))
    y = (pairs[:, 0] > 0).astype(int)
    for count in (3, 5, 7, 10):
        for monotonic in (True, False):
            mapping = opt.learn_bin(pairs, y, count, signed=True, monotonic=monotonic)
            encoded = opt.apply_bin(pairs, mapping)
            assert np.isfinite(encoded).all()
            assert encoded[:, 0] == pytest.approx(-encoded[:, 1])
            values = opt.apply_bin(np.asarray([[np.nan, np.inf], [-1000, 1000]]), mapping)
            assert values[0].tolist() == [0, 0]
            assert values[1, 0] == pytest.approx(-values[1, 1])
            assert all(b["matches"] > 0 for b in mapping["bins"].values())
    constant = opt.learn_bin(np.zeros((len(y), 2)), y, 10, signed=True)
    assert constant["iv"] == 0
    assert constant["edges"] == []


def test_oof_training_block_never_uses_own_label_for_its_map(monkeypatch):
    pairs = np.arange(1, 301, dtype=float)
    x = np.column_stack((pairs, -pairs))[:, :, None]
    dates = np.repeat(["2022-01-01", "2022-07-01", "2022-12-01"], 100)
    labels = (pairs % 3 == 0).astype(int)
    original = opt.fit_maps
    observed = []

    def capture(values, y, *config):
        observed.append(y.copy())
        return original(values, y, *config)

    monkeypatch.setattr(opt, "fit_maps", capture)
    _, _, _, report = opt.fit_logistic(
        x,
        labels,
        dates,
        {"penalty": "l2", "bins": 5, "bin_mode": "monotonic", "oof": True},
        ["diff"],
    )
    assert report["oof_initial_matches"] == 100
    assert observed[0].tolist() == labels.tolist()  # Frozen inference map.
    assert observed[1].tolist() == labels[:100].tolist()  # OOF July–December.


def test_full_sequential_experiment_smoke(tmp_path, monkeypatch):
    from functools import wraps

    rows = [
        Match(
            str(year * 1000 + i),
            date(year, 1, 1) + timedelta(days=i * 2),
            "M",
            "tour",
            "hard" if i % 3 else "clay",
            f"p{i % 12}",
            f"p{(i + 2) % 12}",
            int(i % 3 == 1),
            True,
            str(i),
        )
        for year in range(2021, 2026)
        for i in range(110)
    ]
    original = opt.fit_cat

    @wraps(original)
    def fast(*args, **kwargs):
        train, y, config = args
        return original(train, y, {**config, "iterations": 8}, **kwargs)

    monkeypatch.setattr(opt, "fit_cat", fast)
    reports = {}

    def collect(name, result):
        if name == "E03B":
            assert "E03A" in reports
        reports[name] = result

    opt.optimize(rows, {"fingerprint": "frozen"}, tmp_path, collect)
    assert list(reports) == ["E03A", "E03B"]
    assert {r["common_targets"] for r in reports.values()} == {220}
    assert len(reports["E03B"]["candidates"]) <= 30
    assert reports["E03B"]["feature_audit"]["shap_matches"] > 0
    assert reports["E03A"]["feature_audit"]["vif"]["own/elo.count"] is None
    for report in reports.values():
        assert report["source"]["fingerprint"] == "frozen"
        assert not report["holdout_opened"]
    from dataclasses import replace

    with pytest.raises(ValueError, match="validation-only"):
        opt.optimize([replace(rows[0], day=date(2026, 1, 1))], {}, tmp_path, collect)
