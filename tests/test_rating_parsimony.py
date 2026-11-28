from dataclasses import replace
from datetime import date, timedelta

import pytest

from tennis_predictor.ml.baselines import parsimony as p
from tennis_predictor.ml.ratings.inputs import Match


def test_tolerance_selects_simplest_without_accepting_unstable_fold():
    def item(name, scores, count, depth=0):
        return {
            "config": {"id": name, "depth": depth},
            "inner_log_loss": sum(scores) / len(scores),
            "folds": {
                str(i): {"log_loss": score, "features": list(range(count))}
                for i, score in enumerate(scores)
            },
        }

    best = item("best", [0.6100, 0.6120], 19)
    near = item("near", [0.6104, 0.6124], 10)
    unstable = item("unstable", [0.612, 0.610], 6)
    sensitivity = item("sensitivity", [0.6108, 0.6128], 8)
    assert p.select([best, near, unstable, sensitivity], 0.0005) == near
    assert p.select([best, near, unstable, sensitivity], 0.001) == sensitivity
    assert (
        p.select(
            [item("shallow", [0.6104, 0.6124], 77, 3), item("deep", [0.6100, 0.6120], 8, 4)], 0.0005
        )["config"]["id"]
        == "shallow"
    )


def test_e03c_separates_inner_folds_and_preserves_pairs(tmp_path, monkeypatch):
    orig = p.fit_cat

    def quick(train, y, config, *, exact):
        return orig(train, y, {**config, "iterations": 6}, exact=False)

    monkeypatch.setattr(p, "fit_cat", quick)
    matches = [
        Match(
            str(year * 1000 + i),
            date(year, 1, 1) + timedelta(days=i * 3),
            "M",
            "tour",
            "clay" if i % 2 else "hard",
            f"p{i % 9}",
            f"p{(i + 1) % 9}",
            int(i % 4 < 2),
            True,
            str(i),
        )
        for year in range(2021, 2026)
        for i in range(104)
    ]
    report = p.run(matches, {"fingerprint": "test"}, tmp_path)
    assert report["common_targets"] == 208
    assert not report["holdout_opened"]
    base = report["families"]["logistic"]["candidates"][0]
    assert base["folds"]["2023-01-01"]["train_matches"] == 104
    assert base["folds"]["2023-07-01"]["train_matches"] == 165
    for family in report["families"].values():
        for record in family["outer"].values():
            assert record["metrics"]["all"]["n"] == 208
            assert record["metrics"]["year/2025"]["n"] == 104
    with pytest.raises(ValueError, match="pre-2026"):
        p.run([replace(matches[0], day=date(2026, 1, 1))], {}, tmp_path)
