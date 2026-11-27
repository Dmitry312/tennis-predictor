from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from math import isfinite
from struct import unpack

import pytest

from tennis_predictor.ml.ratings.algorithms import Config, State, inactive, probability, update
from tennis_predictor.ml.ratings.engine import PACK, Reader, build
from tennis_predictor.ml.ratings.evaluate import Splits, evaluate, variants
from tennis_predictor.ml.ratings.inputs import Match, result

DAY = date(2024, 1, 1)


def match(i, offset, left="a", right="b", **changes):
    return replace(
        Match(
            str(i), DAY + timedelta(days=offset), "M", "tour", "hard", left, right, 1, True, str(i)
        ),
        **changes,
    )


def rows(root, table="predictions"):
    with closing(Reader(root).connect()) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()


def test_published_glicko_examples_and_finite_probabilities():
    games = [(State(1400, 30), 1), (State(1550, 100), 0), (State(1700, 300), 0)]
    classic = update("glicko", State(1500, 200), games, Config())
    revised = update("glicko2", State(1500, 200, 0.06), games, Config())
    assert (classic.rating, classic.rd) == pytest.approx((1464.1, 151.4), abs=0.06)
    assert (revised.rating, revised.rd, revised.sigma) == pytest.approx(
        (1464.06, 151.52, 0.05999), abs=0.015
    )
    assert revised.sigma == pytest.approx(0.059996, abs=0.000005)
    left, right = State(1800, 30), State(1100, 250)
    assert probability(left, right) + probability(right, left) == pytest.approx(1)
    assert 0 < probability(State(-1e8), State(1e8)) < 1
    for model in ("glicko", "glicko2"):
        state = update(model, State(10000, 120, 0.06), [(State(-10000, 100), 0)], Config())
        assert all(isfinite(v) for v in (state.rating, state.rd, state.sigma))


def test_daily_states_unknown_surface_and_inactivity(tmp_path):
    matches = [
        match(1, 0, surface=None, target=False),
        match(2, 0, right="c", target=False),
        match(3, 10, right="c"),
        match(4, 10, gender="F"),
    ]
    build(tmp_path, reversed(matches), {"registry_token": "v1"})
    predictions = rows(tmp_path)
    assert all(unpack("<5d", row[12]) == (0.5,) * 5 for row in predictions[:2])
    assert unpack("<5d", predictions[-1][12]) == (0.5,) * 5
    assert PACK.unpack_from(predictions[2][10])[0] == 1524
    with closing(Reader(tmp_path).connect()) as db:
        # No surface updates for the unknown match's exclusive opponent.
        assert db.execute(
            "SELECT count(*) FROM states WHERE player='b' AND model='surface' AND pool='M'"
        ).fetchone() == (0,)
    assert inactive("glicko", State(1500, 100), 25, Config()).rd == pytest.approx(12500**0.5)
    assert inactive("glicko2", State(1500, 100, 0.06), 1500, Config()).rd == 350


def test_replay_incremental_revision_and_correction(tmp_path):
    inc, full = tmp_path / "inc", tmp_path / "full"
    matches = [match(1, 0), match(2, 1, "b", "c"), match(3, 2, "c", "d")]
    pin = {"registry_token": "v1"}
    build(inc, matches[:2], pin)
    initial = build(inc, matches, pin)
    assert initial["days_replayed"] == 1
    assert build(inc, reversed(matches), pin)["version"] == initial["version"]
    changed_pin = {"registry_token": "v2"}
    rebound = build(inc, matches, changed_pin)
    assert rebound["version"] != initial["version"]
    assert rebound["days_replayed"] == 0
    baseline = rows(inc)
    matches[-1] = replace(matches[-1], score=0)
    build(inc, matches, changed_pin)
    assert [row[10:13] for row in rows(inc)] == [row[10:13] for row in baseline]
    matches[0] = replace(matches[0], score=0, revision="corrected")
    correction = build(inc, matches, changed_pin)
    assert correction["days_replayed"] == 3
    assert rows(inc)[2][12] != baseline[2][12]
    assert build(full, reversed(matches), changed_pin)["version"] == correction["version"]
    assert rows(full) == rows(inc)
    assert set(rows(full, "states")) == set(rows(inc, "states"))
    assert build(inc, matches[:-1], changed_pin)["days_reused"] == 2
    assert len(rows(inc)) == 2


def test_result_contract_without_statistics():
    sides = [
        {"side": i, "status": 3, "duplicate_of": None, "player_id": pid}
        for i, pid in enumerate(("a", "b"), 1)
    ]
    wins = [
        {
            "side": i,
            "metric": "match_win",
            "scope": 0,
            "quality": "VALID",
            "history_eligible": True,
            "numerator": 2 - i,
        }
        for i in (1, 2)
    ]
    assert result(sides, wins) == ("a", "b", 1)
    assert result(sides, wins[:1]) is None
    assert result(sides, [*wins, wins[0]]) is None
    assert result([{**sides[0], "status": 4}, sides[1]], wins) is None
    assert result([{**sides[0], "duplicate_of": "other"}, sides[1]], wins) is None
    assert result([{**sides[0], "player_id": None}, sides[1]], wins) is None


def test_evaluation_uses_common_targets_and_quarantines_test(tmp_path):
    splits = Splits(*(DAY + timedelta(days=i) for i in (0, 1, 2, 4, 6)))
    build(tmp_path, [match(i, i, target=i != 3) for i in range(6)], {})
    with closing(Reader(tmp_path).connect()) as db:
        report = evaluate(db, splits)
        test = evaluate(db, splits, held_out=True)
    assert set(report) == {"train", "validation"}
    assert set(test) == {"test"}
    assert all(model["all"]["coverage"] == 0.5 for model in report["validation"].values())
    assert all(model["all"]["n"] == 2 for model in test["test"].values())
    baseline = Config()
    choices = list(variants())
    assert len(choices) == 10
    assert all(
        sum(getattr(config, k) != v for k, v in vars(baseline).items()) == 1
        for _, config, _ in choices[1:]
    )


def test_catalog_match_store_and_registry_contract(tmp_path):
    from pathlib import Path

    from tennis_predictor.ml.aggregation import batch
    from tennis_predictor.ml.canonical_layer import storage
    from tennis_predictor.ml.canonical_layer.identity import PinnedIdentities
    from tennis_predictor.ml.canonical_layer.source import CatalogAdapter
    from tennis_predictor.ml.identity_review import staging
    from tennis_predictor.ml.identity_review.bulk import import_approved_groups
    from tennis_predictor.ml.ratings.inputs import load

    catalog = Path(__file__).resolve().parents[1] / "docs/ml/data_catalog"
    evidence = tmp_path / "evidence"
    registry = tmp_path / "registry"
    dataset = staging.extract(evidence, staging.CatalogSource(catalog))["dataset"]
    with closing(staging.connection(evidence)) as db:
        approved = {
            row[0]: "catalog-" + row[0]
            for row in db.execute("SELECT DISTINCT group_id FROM observations")
        }
    packet = {
        "dataset": dataset,
        "groups": approved,
        "recorded_at": "2026-11-27T07:37:00Z",
        "approval_ref": "catalog test only",
        "parent": "root",
    }
    approval = import_approved_groups(evidence, registry, packet)
    identity = PinnedIdentities(registry, 1)
    mode = "historical_research_replay"
    root = tmp_path / "canonical"
    canonical = storage.build(
        CatalogAdapter(catalog),
        root,
        {"mode": mode, "version": "canonical-v1", "reviews": {}},
        identities=identity,
    )
    batch.build(batch.CanonicalReader(root / mode, canonical["version"]), tmp_path / "facts")
    facts = batch.SnapshotReader(tmp_path / "facts")
    matches, source = load(facts, root / mode, identity)
    assert source["input_matches"] == 20 and len(matches) > 0
    assert source["registry_token"] == approval["token"]
    assert build(tmp_path / "ratings", matches, source)["matches"] == len(matches)
    import_approved_groups(evidence, registry, {**packet, "parent": approval["token"]})
    with pytest.raises(ValueError, match="exact approved registry pin"):
        load(facts, root / mode, PinnedIdentities(registry, 2))


def test_cli_freezes_validation_choices_before_holdout(tmp_path, monkeypatch, capsys):
    import json
    import sys

    from scripts import run_ratings

    stream = [match(i, i, score=i % 2, target=i != 3) for i in range(6)]
    source = {"match_data": "fixture", "registry_token": "approved"}
    monkeypatch.setattr(run_ratings, "SnapshotReader", lambda *_: object())
    monkeypatch.setattr(run_ratings, "PinnedIdentities", lambda *_: object())
    monkeypatch.setattr(run_ratings, "load", lambda *_: (stream, source))
    argv = [
        "run_ratings.py",
        "--match-data",
        "unused",
        "--canonical",
        "unused",
        "--registry",
        "unused",
        "--registry-revision",
        "1",
        "--output",
        str(tmp_path),
        "--splits",
        *(str(DAY + timedelta(days=i)) for i in (0, 1, 2, 4, 6)),
    ]
    monkeypatch.setattr(sys, "argv", [*argv, "--compare"])
    run_ratings.main()
    frozen = capsys.readouterr().out
    validation = json.loads(frozen)
    assert len(validation["selected_on_validation"]) == 5
    assert all(run["matches"] == 4 for run in validation["runs"].values())
    path = tmp_path / "validation.json"
    path.write_text(frozen)
    monkeypatch.setattr(sys, "argv", [*argv, "--held-out", str(path)])
    run_ratings.main()
    final = json.loads(capsys.readouterr().out)
    assert set(final["runs"]) == set(validation["selected_on_validation"].values())
    assert all(set(run["metrics"]) == {"test"} for run in final["runs"].values())
    assert all(run["matches"] == 6 for run in final["runs"].values())
    assert len(rows(tmp_path / "baseline")) == 4
    monkeypatch.setattr(
        run_ratings, "load", lambda *_: (stream, {**source, "registry_token": "new"})
    )
    with pytest.raises(SystemExit):
        run_ratings.main()
