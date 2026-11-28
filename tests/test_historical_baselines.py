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


def test_readonly_details_transport_projects_only_result_evidence():
    from io import BytesIO

    import pyarrow as pa
    import pyarrow.parquet as pq
    from scripts.export_historical_baselines import DETAIL_FIELDS, details

    key, etag = "nbbet_details/gender=M/level=tour/year=2023/a.parquet", '"etag"'
    sample = detail(meta(1))
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pylist([sample]), buffer)
    raw = buffer.getvalue()

    class Store:
        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "existing", "Key": key, "IfMatch": etag}
            return {"ETag": etag, "Body": BytesIO(raw)}

    output = BytesIO()
    details(Store(), "existing", [(key, etag, len(raw))], output)
    obj, rows = map(json.loads, output.getvalue().splitlines())
    assert obj == {"object": [key, etag, len(raw)]}
    assert rows == {"details": [{key: sample[key] for key in DETAIL_FIELDS}]}

    with pytest.raises(ValueError, match="changed after inventory"):
        details(Store(), "existing", [(key, etag, len(raw) + 1)], BytesIO())


def test_historical_export_archive_imports_without_infra_or_ml_dependencies(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "scripts/export_historical_baselines.py", tmp_path / "scripts")
    shutil.copytree(root / "src", tmp_path / "src")
    # Use the workflow archive layout and a stdlib-only interpreter.
    run = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(tmp_path / "scripts/export_historical_baselines.py"),
            "--help",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        check=True,
    )
    assert b"--remote" in run.stdout


def test_receive_writes_real_jsonl_and_sanitizes_protocol_errors(tmp_path, monkeypatch):
    import base64
    import hashlib
    from io import BytesIO

    from scripts import export_historical_baselines as transport
    from scripts.run_historical_baselines import lines

    class Remote:
        def __init__(self, wire, status=0):
            self.stdout = BytesIO(wire)
            self.status = status

        def wait(self, *, timeout):
            return self.status

    data = b"sqlite bytes"
    records = [
        {"stage": "registry"},
        {"registry": {"token": "pin", "revision": 1, "sha256": hashlib.sha256(data).hexdigest()}},
        {"registry_block": base64.b64encode(data).decode()},
        {"stage": "postgres"},
        {"postgres": {"as_of": "2026-11-28T00:00:00Z", "transaction": "snapshot"}},
        {"metadata": [{"match_id": "one", "newlines": "one\ntwo"}]},
        {"details": [{"result": "ok"}]},
        {"done": True},
    ]
    wire = b"".join(json.dumps(row).encode() + b"\n" for row in records)
    good = tmp_path / "good"
    good.mkdir()
    transport.receive(Remote(wire), good)
    assert list(lines(good / "metadata.jsonl")) == records[5]["metadata"]
    assert list(lines(good / "details.jsonl")) == records[6]["details"]
    assert (good / "source.json").exists()

    monkeypatch.setattr(transport, "MAX_FRAME", 128)
    failures = [
        (b'{"stage":"imports"}\n', "EOF at imports; exit=255"),
        (b"x" * 129, "Oversized remote export frame at bootstrap"),
        (b"secret value", "Truncated remote export frame at bootstrap; exit=255"),
        (b"secret value\n", "Invalid remote export frame at bootstrap"),
        (
            b'{"stage":"registry"}\n{"error":"ClientError"}\n',
            "failed at registry: ClientError; exit=255",
        ),
    ]
    for i, (payload, expected) in enumerate(failures):
        folder = tmp_path / str(i)
        folder.mkdir()
        with pytest.raises(ValueError, match=expected) as err:
            transport.receive(Remote(payload, 255), folder)
        assert "secret value" not in str(err.value)
        assert not (folder / "source.json").exists()
