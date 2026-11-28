import json
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest
from scripts.run_historical_baselines import catalog

from tennis_predictor.ml.aggregation import batch
from tennis_predictor.ml.baselines.evaluation import run, supervised, vectors
from tennis_predictor.ml.baselines.projection import project
from tennis_predictor.ml.canonical_layer import storage
from tennis_predictor.ml.canonical_layer.source import CatalogAdapter
from tennis_predictor.ml.ratings.engine import Reader, build
from tennis_predictor.ml.ratings.inputs import Match, load

AS_OF = datetime(2026, 11, 28, tzinfo=UTC)
SCORE = json.dumps([{"score_team1": 6, "score_team2": 2}, {"score_team1": 6, "score_team2": 4}])


def meta(i, **kw):
    return {
        "match_id": str(UUID(int=i)),
        "nbbet_match_id": i,
        "gender": "M",
        "level": "tour",
        "surface": "clay",
        "tournament_name": "ATP Example (Qualifiers)",
        "status": 3,
        "match_date": "2023-01-01",
        "player1": "a",
        "player2": "b",
        **kw,
    }


def detail(row, **kw):
    return {
        "canonical_match_id": row["match_id"],
        "nbbet_match_id": row["nbbet_match_id"],
        "status": 3,
        "sets_json": SCORE,
        "fetched_at": "2023-01-02T10:00:00Z",
        # Metadata surface must prevail regardless of any stale source value.
        "surface": "hard",
        **kw,
    }


class Registry:
    def resolve(self, row, _):
        return tuple({"player_id": row[key]} for key in ("player1", "player2"))


def test_projection_waterfall_unknown_repaired_surface_and_same_day():
    rows = [
        meta(1),
        meta(2, player2="c", surface="unknown"),
        meta(3, player1="d", player2="e", tournament_name="Fed Cup"),
        meta(4, player1="f", player2="g", duplicate_of=str(UUID(int=1))),
        meta(5, player1=None, player2="h"),
        meta(6, match_date="2024-01-01", player1="i", player2="j"),
        meta(7, match_date="2020-01-01"),
        meta(8, match_date="2024-01-01", player1="k", player2="l"),
        meta(9, match_date="2024-01-01", player1="m", player2="n"),
    ]
    evidence = [detail(row) for row in rows[:7]]
    evidence += [detail(rows[5], sets_json="[]"), detail(rows[8], sets_json="[]")]
    matches, report = project(rows, evidence, Registry(), AS_OF)
    assert [(m.surface, m.target) for m in matches] == [("clay", False), (None, False)]
    assert report["targets"] == {"unknown_surface": 1, "nonunique_player_day": 1}
    assert report["results"] == {
        "rated": 2,
        "excluded_tournament_or_doubles": 1,
        "canonical_duplicate": 1,
        "unverified_or_equal_players": 1,
        "conflicting_latest_details": 1,
        "outside_history_or_missing_date": 1,
        "details_missing": 1,
        "invalid_or_missing_score": 1,
    }


def test_checked_in_catalog_projection_parity(tmp_path):
    path = Path(__file__).resolve().parents[1] / "docs/ml/data_catalog"
    metadata, details, identity, as_of, _ = catalog(path, tmp_path)
    expected, _ = project(metadata, details, identity, as_of, start=date(2016, 1, 1))
    canonical = storage.build(
        CatalogAdapter(path),
        tmp_path / "canonical",
        {"mode": "historical_research_replay", "version": "canonical-v1", "reviews": {}},
        identities=identity,
    )
    root = tmp_path / "canonical/historical_research_replay"
    batch.build(batch.CanonicalReader(root, canonical["version"]), tmp_path / "facts")
    normal, _ = load(batch.SnapshotReader(tmp_path / "facts"), root, identity)

    def comparable(rows):
        return {
            (r.match_id, r.day, r.gender, r.level, r.surface, r.left, r.right, r.score)
            for r in rows
        }

    assert len(normal) == 7
    assert comparable(expected) == comparable(normal)


def synthetic():
    return [
        Match(
            str(i),
            date(year, month, 1),
            "M" if i % 2 else "F",
            "tour",
            "hard" if i % 3 else "clay",
            "a" if i % 2 else "b",
            "c" if i % 2 else "d",
            i % 3 != 0,
            True,
            str(i),
        )
        for i, (year, month) in enumerate(
            (year, month) for year in range(2021, 2027) for month in range(1, 7)
        )
    ]


def test_past_only_validation_and_symmetric_models(tmp_path):
    matches = synthetic()
    first = run(matches, {"token": "frozen"}, tmp_path / "validation", AS_OF.date())
    changed = [m for m in matches if m.day.year < 2026]
    second = run(changed, {"token": "frozen"}, tmp_path / "validation", AS_OF.date())
    assert first == second
    assert first["common_targets"] == 12
    for model in ("logistic", "catboost"):
        folds = first["runs"]["baseline"][model]["folds"]
        assert [fold["train"] for fold in folds] == [12, 18]
        assert [fold["scored"] for fold in folds] == [6, 6]
    final = run(matches, {"token": "frozen"}, tmp_path / "holdout", AS_OF.date(), frozen=first)
    assert final["common_targets"] == 6
    target = tmp_path / "features"
    build(target, matches, {})
    with closing(Reader(target).connect()) as db:
        source = db.execute("SELECT * FROM predictions ORDER BY day").fetchall()
    row = list(source[-1])
    reverse = list(row)
    reverse[10], reverse[11] = row[11], row[10]
    reverse[14], reverse[15] = row[15], row[14]
    import struct

    reverse[12] = struct.pack("<5d", *(1 - p for p in struct.unpack("<5d", row[12])))
    single, other = vectors(row)
    assert vectors(reverse)[0] == other
    assert vectors(reverse)[1] == pytest.approx(single)
    for model in ("logistic", "catboost"):
        left, right = supervised(model, source[:-1], [row, reverse])
        assert left + right == pytest.approx(1)
