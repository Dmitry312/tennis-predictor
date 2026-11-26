"""Offline integration tests for the canonical Parquet layer."""

import json
import shutil
import socket
from dataclasses import replace
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from scripts.audit_ml_catalog import audit

from tennis_predictor.ml.canonical_layer import storage
from tennis_predictor.ml.canonical_layer.contracts import (
    METRICS,
    SCHEMAS,
    VERSION,
    temporal_gate,
)
from tennis_predictor.ml.canonical_layer.evidence import digest, encoded, fingerprint, instant
from tennis_predictor.ml.canonical_layer.source import CatalogAdapter
from tennis_predictor.ml.canonical_layer.transform import MatchTransform

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "docs/ml/data_catalog"
CONFIG = {"version": VERSION, "mode": "strict_as_of", "prediction_cutoff": None, "reviews": {}}
MATCH = "00035326-4450-5d09-b060-86a1f6851e63"


def snapshot(output, report):
    return output / report["mode"] / "versions" / report["version"]


def table(folder, name):
    return pq.read_table(folder / f"{name}.parquet").to_pylist()


def test_all_real_examples_and_no_network(tmp_path, monkeypatch):
    def network(*args, **kwargs):
        pytest.fail("canonical ETL attempted a network connection")

    monkeypatch.setattr(socket, "socket", network)
    monkeypatch.setattr(socket, "create_connection", network)
    monkeypatch.setattr(socket, "getaddrinfo", network)
    adapter = CatalogAdapter(CATALOG)
    first = storage.build(adapter, tmp_path, CONFIG)
    folder = snapshot(tmp_path, first)
    assert first["processed_matches"] == 20
    assert first["changes"] == {"new": 20, "changed": 0, "skipped": 0, "removed": 0}
    assert first["rows"] == {
        "players": 40,
        "player_aliases": 50,
        "tournaments": 0,
        "matches": 20,
        "match_results": 20,
        "match_player_stats": 50,
        "match_player_pbp": 1350,
        "player_rankings": 10,
        "match_odds": 4,
        "data_quality": first["rows"]["data_quality"],
        "data_manifest": 100,
    }
    assert first["source_rows"] == {
        "matches": 20,
        "nbbet_details": 9,
        "nbbet_stats": 25,
        "pbp": 1350,
        "nbbet_raw": 5,
    }
    assert "pbp" in first["quality_by_source"]["pbp"]
    assert first["candidates"] == first["match_local_winners"] == 7
    assert first["confirmed_results"] == 7 and first["eligible_targets"] == 0
    expected_sides = {318197: 2, 730766: 2, 368682: 1, 750496: 2, 531103: 1, 207964: 1, 719212: 1}
    match_ids = {r["match_id"]: r["source_match_id"] for r in table(folder, "matches")}
    results = table(folder, "match_results")
    assert len(results) == len(match_ids) == 20
    for row in results:
        expected = expected_sides.get(match_ids[row["match_id"]])
        assert row["candidate_winner_side"] == row["winner_side"] == expected
        assert row["target_p1_win"] == (int(expected == 1) if expected else None)
        assert row["target_eligible"] is False and row["player_state_eligible"] is False
        if expected is not None:
            assert row["status"] == 3 and row["orientation"] == "same"
        elif row["sets_raw"] is None:
            assert row["exclusion_reason"] == "score_observation_missing"
        else:
            assert row["status"] == 9 and row["exclusion_reason"] == "non_completed_match_status"
    # These four winning sides have no player-name payload in the details record.
    missing_names = {730766, 750496, 531103, 719212}
    for mid, sha in adapter.entries():
        if match_ids[mid] in missing_names:
            detail = next(
                obs.rows[0]
                for obs in adapter.load(mid, sha).observations
                if obs.source == "nbbet_details" and obs.rows
            )
            assert not detail.get("players_json")
    assert first["player_observations"] == {"provisional": 40}
    for name, schema in SCHEMAS.items():
        assert pq.read_schema(folder / f"{name}.parquet").equals(schema, check_metadata=True)
        assert len(table(folder, name)) == first["rows"][name]
    observations = table(folder, "data_manifest")
    manifest_ids = {row["manifest_id"] for row in observations}
    checks = table(folder, "data_quality")
    assert len({row["quality_id"] for row in checks}) == len(checks)
    assert all(row["manifest_id"] in manifest_ids for row in checks)
    assert all(row["available_at"] is None for row in table(folder, "match_results"))
    assert any(
        row["game_complete_source"] and not row["game_complete_verified"]
        for row in table(folder, "match_player_pbp")
    )
    assert all(row["model_feature_eligible"] is False for row in table(folder, "match_odds"))
    assert not any(row["feature_eligible"] for row in checks)
    assert (folder / "quality_report.json").exists()

    def no_reload(*args):
        pytest.fail("unchanged source was transformed")

    monkeypatch.setattr(adapter, "load", no_reload)
    second = storage.build(adapter, tmp_path, CONFIG)
    assert first["version"] == second["version"]
    assert second["changes"] == {"new": 0, "changed": 0, "skipped": 20, "removed": 0}
    assert second["invalidation"]["earliest_date"] is None


def reviewed_bundle():
    adapter = CatalogAdapter(CATALOG)
    bundle = adapter.load(MATCH, dict(adapter.entries())[MATCH])
    by_source = {obs.source: obs for obs in bundle.observations}
    matches, details = by_source["matches"], by_source["nbbet_details"]
    result_ref = MatchTransform.ref(details, 0)
    config = {
        **CONFIG,
        "prediction_cutoff": "2026-12-01T00:00:00Z",
        "reviews": {
            MATCH: {
                "bundle_digest": bundle.manifest["bundle_digest"],
                "players": {
                    str(side): {
                        "identity_key": f"00000000-0000-4000-8000-00000000000{side}",
                        "evidence_ref": f"review/player/{side}",
                        "recorded_at": "2021-01-01T00:00:00Z",
                    }
                    for side in (1, 2)
                },
                "tournament": {
                    "edition_id": "00000000-0000-4000-8000-000000000003",
                    "tournament_id": "00000000-0000-4000-8000-000000000004",
                    "best_of": 3,
                    "decider_rule": "tiebreak7",
                    "evidence_ref": "review/edition",
                },
                "compatible_versions": [[matches.version, details.version]],
                "availability": {
                    result_ref: {
                        "available_at": "2026-01-01T00:00:00Z",
                        "evidence_ref": "review/result/publication",
                    }
                },
                "results": {
                    result_ref: {
                        "independent_winner_side": 1,
                        "evidence_ref": "review/result/winner",
                    }
                },
            }
        },
    }
    return bundle, by_source, config


def test_confirmed_result_survives_missing_pbp_and_bad_break_points():
    bundle, observations, config = reviewed_bundle()
    config["reviews"][MATCH].pop("results")
    stats = observations["nbbet_stats"]
    scoped = [dict(row, break_points_won_p1=5, break_points_all_p1=1) for row in stats.rows]
    broken_break_points = replace(stats, rows=scoped)
    no_pbp = replace(observations["pbp"], rows=[])
    bundle = replace(
        bundle,
        observations=tuple(
            {"pbp": no_pbp, "nbbet_stats": broken_break_points}.get(obs.source, obs)
            for obs in bundle.observations
        ),
    )
    data = MatchTransform(bundle, config).run()
    assert data["match_results"][0]["target_p1_win"] == 1
    assert data["match_results"][0]["player_state_eligible"] is True
    assert not data["match_player_pbp"]
    assert set(METRICS) <= data["match_player_stats"][0].keys()
    checks = {(row["category"], row["status"]) for row in data["data_quality"]}
    assert {
        ("pbp", "missing"),
        ("break_points", "conflict"),
        ("serve", "valid"),
        ("return", "valid"),
        ("identity", "valid"),
    } <= checks


def test_quarantine_and_provisional_identity():
    bundle, observations, _ = reviewed_bundle()
    meta = observations["matches"]
    same = replace(meta, rows=[dict(meta.rows[0], player2=meta.rows[0]["player1"].upper())])
    ambiguous = replace(bundle, observations=(same, *bundle.observations[1:]))
    data = MatchTransform(ambiguous, CONFIG).run()
    assert all(row["resolution_status"] == "quarantine" for row in data["players"])
    assert data["matches"][0]["player1_id"] is None
    assert data["matches"][0]["player2_id"] is None
    assert data["match_results"][0]["target_p1_win"] == 1
    assert data["match_results"][0]["target_eligible"] is False
    normal = MatchTransform(bundle, CONFIG).run()
    assert all(
        r["resolution_status"] == "provisional" and r["recorded_at"] is None
        for r in normal["players"]
    )


def test_tally_infers_format_but_reviewed_format_still_constrains_result():
    bundle, _, config = reviewed_bundle()
    strict = MatchTransform(bundle, CONFIG).run()["match_results"][0]
    assert strict["source_candidate_winner_side"] == 1
    assert strict["winner_side"] == 1
    assert strict["best_of"] == 3
    assert strict["decider_rule"] is None
    assert strict["target_p1_win"] == 1 and not strict["target_eligible"]
    assert strict["sets"][0]["duration_raw"] is not None
    review = config["reviews"][MATCH]
    review["tournament"]["best_of"] = 5
    five = MatchTransform(bundle, config).run()["match_results"][0]
    assert five["winner_side"] is None and five["target_p1_win"] is None
    assert five["exclusion_reason"] == "incomplete_match"


def test_temporal_modes_and_forbidden_inputs():
    day = date(2020, 1, 1)
    cutoff = instant("2020-01-04T00:00:00Z")
    recent = instant("2020-01-01T20:00:00Z")
    future = instant("2020-02-01T00:00:00Z")
    strict = {"mode": "strict_as_of", "cutoff": cutoff}
    replay = {"mode": "historical_research_replay", "cutoff": cutoff}
    assert temporal_gate("result", **strict, available_at=recent, event_date=day)[0]
    assert not temporal_gate("result", **strict, event_date=day)[0]
    assert temporal_gate("result", **replay, event_date=day)[0]
    assert temporal_gate("result", **replay, event_date=day, available_at=future) == (
        True,
        "assumed_after_match_date",
    )
    assert temporal_gate(
        "result", mode=replay["mode"], cutoff=instant("2020-01-02T00:00:00Z"), event_date=day
    )[0]
    assert not temporal_gate("result", mode=replay["mode"], cutoff=recent, event_date=day)[0]
    assert not temporal_gate("ranking", **replay, available_at=recent)[0]
    for kind in ("odds", "recommendations"):
        assert not temporal_gate(kind, **strict, available_at=recent)[0]


def copy_matches(destination, *ids):
    indexes = json.loads((CATALOG / "index.json").read_bytes())
    indexes["real_examples"] = [e for e in indexes["real_examples"] if e["match_id"] in ids]
    destination.mkdir()
    for entry in indexes["real_examples"]:
        shutil.copytree(CATALOG / entry["path"], destination / entry["path"])
    (destination / "index.json").write_bytes(encoded(indexes))
    return CatalogAdapter(destination)


def edit_source(catalog, mid, source, change):
    directory = catalog / "samples" / f"match_{mid}"
    path = directory / f"{source}.json"
    data = json.loads(path.read_bytes())
    change(data)
    path.write_bytes(encoded(data))
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"][path.name] = digest(path.read_bytes())
    manifest.pop("bundle_digest")
    manifest["bundle_digest"] = fingerprint(manifest)
    manifest_path.write_bytes(encoded(manifest))
    index_path = catalog / "index.json"
    index = json.loads(index_path.read_bytes())
    entry = next(e for e in index["real_examples"] if e["match_id"] == mid)
    entry["manifest_sha256"] = digest(manifest_path.read_bytes())
    index_path.write_bytes(encoded(index))


def test_corrections_and_multiple_versions(tmp_path, monkeypatch):
    other = "00015aa9-3aec-5c74-a4cc-530822107e82"
    adapter = copy_matches(tmp_path / "catalog", MATCH, other)
    output = tmp_path / "output"
    first = storage.build(adapter, output, CONFIG)
    first_path = snapshot(output, first)
    original = table(first_path, "match_results")

    def adjust_details(data):
        old = data["versions"][0]["rows"][0]
        sets = json.loads(old["sets_json"])
        for row in sets:
            row["score_team1"], row["score_team2"] = row["score_team2"], row["score_team1"]
        data["versions"][0]["rows"].append(
            dict(
                old,
                sets_json=json.dumps(sets),
                fetched_at="2026-11-01T00:00:00Z",
            )
        )

    def adjust_stats(data):
        old = data["versions"][0]["rows"][0]
        data["versions"][0]["rows"].append(
            dict(
                old,
                aces_p1=3,
                fetched_at="2026-11-01T00:00:00Z",
            )
        )

    def add_pbp_object(data):
        prior = data["versions"][0]
        data["versions"].append(dict(prior, version_id="different-object", rows=prior["rows"][:8]))

    edit_source(adapter.root, MATCH, "nbbet_details", adjust_details)
    edit_source(adapter.root, MATCH, "nbbet_stats", adjust_stats)
    edit_source(adapter.root, MATCH, "pbp", add_pbp_object)
    load = adapter.load
    loaded = []

    def track(mid, sha):
        loaded.append(mid)
        return load(mid, sha)

    monkeypatch.setattr(adapter, "load", track)
    updated = storage.build(adapter, output, CONFIG)
    update_path = snapshot(output, updated)
    assert loaded == [MATCH]
    assert updated["changes"] == {"new": 0, "changed": 1, "skipped": 1, "removed": 0}
    affected = updated["invalidation"]
    assert affected["match_ids"] == [MATCH]
    assert affected["earliest_date"] and len(affected["player_ids"]) == 2
    assert table(first_path, "match_results") == original
    corrected = [r for r in table(update_path, "match_results") if r["match_id"] == MATCH]
    assert {row["candidate_winner_side"] for row in corrected} == {1, 2}
    assert {row["winner_side"] for row in corrected} == {1, 2}
    assert len({row["source_version"] for row in corrected}) == 2
    checks = [r for r in table(update_path, "data_quality") if r["match_id"] == MATCH]
    assert not any(
        r["reason"]
        in {
            "invalid_or_divergent_scope",
            "invalid_or_divergent_point_key",
            "conflicting_result_evidence",
        }
        for r in checks
    )
    assert any(r["reason"] == "compatible_result_unproven" for r in checks)
    assert updated["rows"]["match_player_pbp"] == first["rows"]["match_player_pbp"] + 8
    archived = next(row for row in audit(adapter.root)["matches"] if row["match_id"] == MATCH)
    assert len(archived["pbp_versions"]) == 2
    assert len(archived["results"]) == 2
    assert not any(f["code"] == "duplicate_stat_scope" for f in archived["findings"])
    assert not any(
        f["code"] == "duplicate_point"
        for version in archived["pbp_versions"]
        for f in version["findings"]
    )


def test_divergent_duplicates_in_same_version_are_flagged():
    bundle, observations, _ = reviewed_bundle()
    stats = observations["nbbet_stats"]
    pbp = observations["pbp"]
    bad_stats = replace(stats, row_indices=(), rows=[*stats.rows, dict(stats.rows[0], aces_p1=88)])
    bad_pbp = replace(pbp, row_indices=(), rows=[*pbp.rows, dict(pbp.rows[0], point_winner=3)])
    bundle = replace(
        bundle,
        observations=tuple(
            {"pbp": bad_pbp, "nbbet_stats": bad_stats}.get(o.source, o) for o in bundle.observations
        ),
    )
    quality = MatchTransform(bundle, CONFIG).run()["data_quality"]
    assert {"invalid_or_divergent_scope", "invalid_or_divergent_point_key"} <= {
        r["reason"] for r in quality if r["status"] == "conflict"
    }


def test_atomic_failure_and_integrity_checks(tmp_path, monkeypatch):
    adapter = copy_matches(tmp_path / "catalog", MATCH)
    output = tmp_path / "output"
    first = storage.build(adapter, output, CONFIG)
    root = output / CONFIG["mode"]

    def fail(*args):
        raise OSError("simulated Parquet write failure")

    monkeypatch.setattr(storage, "write_outputs", fail)
    changed = {**CONFIG, "prediction_cutoff": "2020-01-01T00:00:00Z"}
    with pytest.raises(OSError, match="simulated"):
        storage.build(adapter, output, changed)
    assert storage.read_pointer(root / "CURRENT")["version"] == first["version"]
    assert {path.name for path in (root / "versions").iterdir()} == {first["version"]}
    assert not list(root.glob(".stage-*"))
    input_path = adapter.root / "samples" / f"match_{MATCH}" / "pbp.json"
    input_path.write_bytes(input_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="checksum mismatch"):
        storage.build(adapter, output, CONFIG)


def test_modes_publish_separately(tmp_path):
    adapter = copy_matches(tmp_path / "catalog", MATCH)
    strict = storage.build(adapter, tmp_path / "output", CONFIG)
    replay = storage.build(
        adapter, tmp_path / "output", {**CONFIG, "mode": "historical_research_replay"}
    )
    assert strict["version"] != replay["version"]
    assert snapshot(tmp_path / "output", strict).is_dir()
    assert snapshot(tmp_path / "output", replay).is_dir()


def test_new_and_removed_matches_are_invalidated(tmp_path):
    other = "00015aa9-3aec-5c74-a4cc-530822107e82"
    output = tmp_path / "output"
    initial = storage.build(copy_matches(tmp_path / "initial", MATCH), output, CONFIG)
    added = storage.build(copy_matches(tmp_path / "added", MATCH, other), output, CONFIG)
    assert added["changes"] == {"new": 1, "changed": 0, "skipped": 1, "removed": 0}
    assert added["invalidation"]["match_ids"] == [other]
    assert added["rows"]["matches"] == 2
    restored = storage.build(copy_matches(tmp_path / "restored", MATCH), output, CONFIG)
    assert restored["version"] == initial["version"]
    assert restored["changes"] == {"new": 0, "changed": 0, "skipped": 1, "removed": 1}
    assert restored["invalidation"]["match_ids"] == [other]
    assert snapshot(output, added).is_dir()


def test_pbp_reconciliation_requires_compatible_versions():
    bundle, observations, config = reviewed_bundle()
    points = observations["pbp"]
    details = observations["nbbet_details"]
    review = config["reviews"][MATCH]
    limited = replace(points, rows=points.rows[:2], row_indices=points.row_indices[:2])
    bundle = replace(
        bundle, observations=tuple(limited if o.source == "pbp" else o for o in bundle.observations)
    )
    quality = MatchTransform(bundle, config).run()["data_quality"]
    assert any(r["reason"] == "compatible_result_unproven" for r in quality)
    review["compatible_versions"].append([points.version, details.version])
    data = MatchTransform(bundle, config).run()
    assert any(r["reason"] == "observed_games_below_result" for r in data["data_quality"])
    assert data["match_results"][0]["target_p1_win"] == 1


def file_hashes(folder):
    return {
        str(path.relative_to(folder)): storage.checksum(path)
        for path in folder.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("mode", ["strict_as_of", "historical_research_replay"])
def test_clean_and_incremental_snapshots_are_byte_identical_across_clocks(
    tmp_path, monkeypatch, mode
):
    class Clock:
        value = instant("2019-01-01T00:00:00Z")

        @staticmethod
        def now(tz):
            return Clock.value.astimezone(tz)

    monkeypatch.setattr(storage, "datetime", Clock)
    _, _, config = reviewed_bundle()
    config["mode"] = mode
    config["reviews"][MATCH]["decision_at"] = "2026-11-26T00:00:00Z"
    other = "00015aa9-3aec-5c74-a4cc-530822107e82"
    adapter = copy_matches(tmp_path / "catalog", MATCH, other)
    clean_early = tmp_path / "clean-early"
    first = storage.build(adapter, clean_early, config)
    incremental_root = tmp_path / "incremental"
    subset = storage.build(copy_matches(tmp_path / "subset", MATCH), incremental_root, config)

    Clock.value = instant("2030-01-01T00:00:00Z")
    clean_late = tmp_path / "clean-late"
    second = storage.build(adapter, clean_late, config)
    incremental = storage.build(adapter, incremental_root, config)
    assert first["published_at"] != second["published_at"] == incremental["published_at"]
    assert first["version"] == second["version"] == incremental["version"]
    assert incremental["changes"] == {"new": 1, "changed": 0, "skipped": 1, "removed": 0}
    first_dir = snapshot(clean_early, first)
    incremental_dir = snapshot(incremental_root, incremental)
    assert file_hashes(first_dir) == file_hashes(snapshot(clean_late, second))
    assert file_hashes(first_dir) == file_hashes(incremental_dir)
    assert (incremental_dir / "shards" / MATCH / "matches.parquet").samefile(
        snapshot(incremental_root, subset) / "shards" / MATCH / "matches.parquet"
    )
    result = next(row for row in table(first_dir, "match_results") if row["match_id"] == MATCH)
    assert result["target_p1_win"] == 1 and result["player_state_eligible"]
    assert result["recorded_at"] == instant("2026-11-26T00:00:00Z")
    assert result["available_at"] == instant("2026-01-01T00:00:00Z")
    quality = json.loads((first_dir / "quality_report.json").read_bytes())
    assert {"published_at", "changes", "invalidation"}.isdisjoint(quality)
    for row in table(first_dir, "players"):
        expected = (
            None if row["resolution_status"] == "provisional" else instant("2021-01-01T00:00:00Z")
        )
        assert row["recorded_at"] == expected


def test_decision_time_and_receipt_bound_reviewed_availability():
    bundle, observations, config = reviewed_bundle()
    review = config["reviews"][MATCH]
    review["decision_at"] = "2025-01-01T00:00:00Z"
    result = MatchTransform(bundle, config).run()["match_results"][0]
    assert result["target_p1_win"] == 1 and not result["target_eligible"]
    assert result["winner_side"] == 1
    assert result["exclusion_reason"] == "conflicting_result_availability"

    review.pop("decision_at")
    detail = observations["nbbet_details"]
    without_receipt = replace(detail, rows=[dict(row, fetched_at=None) for row in detail.rows])
    bundle = replace(
        bundle,
        observations=tuple(
            without_receipt if obs.source == detail.source else obs for obs in bundle.observations
        ),
    )
    result = MatchTransform(bundle, config).run()["match_results"][0]
    assert result["recorded_at"] is None and result["available_at"] is not None
    assert result["target_p1_win"] == 1 and not result["target_eligible"]
    assert result["exclusion_reason"] == "availability_upper_bound_unknown"


def score_observation(bundle, detail, **changes):
    replacement = replace(detail, rows=[dict(detail.rows[0], **changes)])
    return replace(
        bundle,
        observations=tuple(
            replacement
            if obs is detail
            else replace(obs, rows=[], row_indices=())
            if obs.source == "nbbet_raw"
            else obs
            for obs in bundle.observations
        ),
    )


@pytest.mark.parametrize(
    ("set_winners", "best_of"),
    [
        ((1, 1), 3),
        ((2, 1, 1), 3),
        ((1, 1, 1), 5),
        ((1, 2, 1, 1), 5),
        ((2, 2, 1, 1, 1), 5),
    ],
)
@pytest.mark.parametrize("side", [1, 2])
def test_legal_terminal_tallies_without_review_or_names(set_winners, best_of, side):
    bundle, observations, _ = reviewed_bundle()
    scores = [
        {"score_team1": 6 if winner == side else 3, "score_team2": 3 if winner == side else 6}
        for winner in set_winners
    ]
    changed = score_observation(
        bundle, observations["nbbet_details"], sets_json=json.dumps(scores), players_json=None
    )
    result = MatchTransform(changed, CONFIG).run()["match_results"][0]
    assert result["winner_side"] == side and result["best_of"] == best_of
    assert result["target_p1_win"] == int(side == 1) and not result["player_state_eligible"]


@pytest.mark.parametrize(
    ("status", "scores"),
    [
        (9, [(6, 0), (6, 0)]),
        (8, [(6, 0), (6, 0)]),
        (3, [(6, 0), (5, 4)]),
        (3, [(6, 0), (4, 6)]),
        (3, [(6, 0), (6, 0), (4, 6)]),
        (3, [(6, 6), (6, 0)]),
        (3, [(6, 0), (6, 0), (10, 8)]),
        (3, []),
    ],
)
def test_no_winner_for_unfinished_illegal_or_missing_scores(status, scores):
    bundle, observations, _ = reviewed_bundle()
    changed = score_observation(
        bundle,
        observations["nbbet_details"],
        status=status,
        sets_json=json.dumps([{"score_team1": a, "score_team2": b} for a, b in scores]),
    )
    result = MatchTransform(changed, CONFIG).run()["match_results"][0]
    assert result["winner_side"] is None
    if not scores:
        assert result["exclusion_reason"] == "score_observation_missing"


def test_winner_rejects_wrong_nbbet_id_and_contradictory_review():
    bundle, observations, config = reviewed_bundle()
    detail = observations["nbbet_details"]
    changed = score_observation(bundle, detail, nbbet_match_id=1)
    result = MatchTransform(changed, CONFIG).run()["match_results"][0]
    assert result["candidate_winner_side"] is None and result["winner_side"] is None
    assert result["exclusion_reason"] == "conflicting_result_evidence"

    review = config["reviews"][MATCH]["results"][MatchTransform.ref(detail, 0)]
    review["independent_winner_side"] = 2
    result = MatchTransform(bundle, config).run()["match_results"][0]
    assert result["candidate_winner_side"] == 1 and result["winner_side"] is None
    assert result["exclusion_reason"] == "independent_result_disagrees"


def test_compatible_raw_disagreement_blocks_match_local_winner():
    bundle, observations, _ = reviewed_bundle()
    detail = observations["nbbet_details"]
    conflicting = replace(
        detail,
        rows=[
            dict(
                detail.rows[0],
                sets_json=json.dumps(
                    [
                        {"score_team1": 0, "score_team2": 6},
                        {"score_team1": 0, "score_team2": 6},
                    ]
                ),
            )
        ],
    )
    bundle = replace(
        bundle,
        observations=tuple(conflicting if obs is detail else obs for obs in bundle.observations),
    )
    result = MatchTransform(bundle, CONFIG).run()["match_results"][0]
    assert result["candidate_winner_side"] == 2 and result["winner_side"] is None
    assert result["exclusion_reason"] == "conflicting_result_evidence"
