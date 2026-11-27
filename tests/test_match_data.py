"""Offline coverage of the Match Data Store boundary and aggregation rules."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from tennis_predictor.ml.aggregation import store
from tennis_predictor.ml.aggregation.batch import CanonicalReader, SnapshotReader, build
from tennis_predictor.ml.aggregation.contracts import historical_before, rate
from tennis_predictor.ml.aggregation.engine import MatchAggregation
from tennis_predictor.ml.canonical_layer.contracts import METRICS
from tennis_predictor.ml.canonical_layer.source import CatalogAdapter
from tennis_predictor.ml.canonical_layer.storage import build as build_canonical

CATALOG = Path(__file__).resolve().parents[1] / "docs/ml/data_catalog"
MATCH = "00035326-4450-5d09-b060-86a1f6851e63"
OTHER = "00015aa9-3aec-5c74-a4cc-530822107e82"
CONFIG = {"version": "canonical-v1", "mode": "strict_as_of", "reviews": {}}


@pytest.fixture(scope="module")
def canonical(tmp_path_factory):
    output = tmp_path_factory.mktemp("canonical")
    report = build_canonical(CatalogAdapter(CATALOG), output, CONFIG)
    return CanonicalReader(output / CONFIG["mode"], report["version"])


def input_match(canonical):
    return canonical.load(MATCH, dict(canonical.entries())[MATCH])


def metric(rows, name, side=1, scope=0):
    return next(
        r for r in rows if r["metric"] == name and r["side"] == side and r["scope"] == scope
    )


def hash_tree(path):
    return {
        p.relative_to(path): store.digest(p.read_bytes()) for p in path.rglob("*") if p.is_file()
    }


class Selection:
    def __init__(self, version, *matches):
        self.version = version
        self.matches = {m.match_id: m for m in matches}
        self.loaded = []

    def entries(self):
        return ((mid, match.input_revision) for mid, match in self.matches.items())

    def load(self, mid, revision):
        self.loaded.append(mid)
        assert self.matches[mid].input_revision == revision
        return self.matches[mid]


def test_real_catalog_snapshot(canonical, tmp_path):
    first = build(canonical, tmp_path)
    assert first["matches"] == 20 and first["match_sides"] == 40
    assert first["processed"] == 20
    reader = SnapshotReader(tmp_path)
    assert reader.manifest["canonical_snapshot"] == canonical.version
    rows = [row for mid in reader.manifest["matches"] for row in reader.match(mid)["metrics"]]
    assert {m["metric"] for m in rows if m["family"] == "stats"} >= set(METRICS)
    assert sum(r["metric"] == "match_win" and r["numerator"] == 1 for r in rows) == 7
    assert not any(r["player_id"] for r in rows)
    assert not any(r["family"] in ("rankings", "odds") for r in rows)
    assert "PARTIAL" in first["quality"] and "CONFLICT" in first["quality"]
    assert "nbbet_duration_hhmm" in (reader.folder / "matches.txt").read_text()
    second = build(canonical, tmp_path)
    assert second["version"] == first["version"]
    assert second["reused"] == 20 and not second["invalidated"]


def test_independent_service_returns_and_pbp(canonical):
    match = input_match(canonical)
    rows = MatchAggregation(match).run()["metrics"]
    first_return = metric(rows, "first_return_win_rate")
    opponent = metric(rows, "first_serve_win_rate", side=2)
    assert first_return["quality"] == opponent["quality"] == "VALID"
    assert first_return["numerator"] == opponent["denominator"] - opponent["numerator"]
    assert first_return["denominator"] == opponent["denominator"]
    assert metric(rows, "games_won")["numerator"] == 12
    assert any(r["family"] == "pbp" and r["quality"] == "PARTIAL" for r in rows)
    assert all(not r["history_eligible"] for r in rows if r["family"] == "pbp")
    assert metric(rows, "duration_minutes", scope=1)["numerator"] == 34
    assert metric(rows, "duration_minutes")["numerator"] == 55
    assert metric(rows, "break_points_saved", side=2)["numerator"] == 3
    assert metric(rows, "break_points_faced", side=2)["numerator"] == 8
    assert metric(rows, "max_games_in_row")["quality"] == "VALID"
    assert metric(rows, "max_games_in_row")["numerator"] == 7
    assert metric(rows, "winners")["quality"] == "MISSING"

    changed = deepcopy(match.tables)
    changed["player_aliases"] = [
        r for r in changed["player_aliases"] if "players_json" not in r["evidence_ref"]
    ]
    rows = MatchAggregation(replace(match, tables=changed)).run()["metrics"]
    assert metric(rows, "first_return_win_rate")["quality"] == "VALID"
    assert metric(rows, "first_serve_win_rate")["quality"] == "VALID"
    assert metric(rows, "first_serve_win_rate")["history_eligible"]

    changed["match_player_stats"] = []
    rows = MatchAggregation(replace(match, tables=changed)).run()["metrics"]
    assert metric(rows, "aces")["numerator"] is None
    assert metric(rows, "aces")["quality"] == "MISSING"
    assert metric(rows, "match_win")["numerator"] == 1


def test_scope_conflict_and_unselected_versions(canonical):
    match = input_match(canonical)
    changed = deepcopy(match.tables)
    first = next(
        r for r in changed["match_player_stats"] if r["scope"] == 0 and r["player_side"] == 1
    )
    changed["match_player_stats"].append({**first, "aces": 999})
    rows = MatchAggregation(replace(match, tables=changed)).run()["metrics"]
    assert metric(rows, "aces")["quality"] == "CONFLICT"
    assert metric(rows, "aces", side=2)["quality"] == "VALID"
    assert metric(rows, "match_win")["quality"] == "VALID"

    changed["match_player_stats"][-1]["source_version"] = "other-source"
    rows = MatchAggregation(replace(match, tables=changed)).run()["metrics"]
    assert not any(r["history_eligible"] for r in rows if r["family"] in ("stats", "return"))


def test_ratio_pooling_rejects_missing_denominators_and_overlaps():
    row = {"metric": "first_serve_win_rate", "quality": "VALID", "scope": 0, "game": None}
    first = {**row, "match_id": "a", "numerator": 2, "denominator": 10}
    second = {**row, "match_id": "b", "numerator": 3, "denominator": 4}
    assert rate(iter([first, second])) == 5 / 14
    assert rate([first, {**second, "denominator": None}]) is None
    assert rate([first, {**second, "scope": 1}]) is None
    assert rate([first, first]) is None


def verified_match(match):
    tables = deepcopy(match.tables)
    tables["players"][0]["resolution_status"] = "verified"
    tables["players"][0]["recorded_at"] = datetime.combine(
        tables["matches"][0]["match_date"] - timedelta(days=1), datetime.min.time(), UTC
    )
    pid = tables["matches"][0]["player1_id"]
    assert tables["players"][0]["player_id"] == pid
    return replace(match, input_revision="verified", tables=tables), pid


def test_live_atomic_processing_duplicate_correction_and_missing_identity(
    canonical, tmp_path, monkeypatch
):
    db = store.MatchDataStore(tmp_path / "facts.sqlite")
    match, player_id = verified_match(input_match(canonical))
    day = match.tables["matches"][0]["match_date"]
    tick = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
    monkeypatch.setattr(store, "clock", lambda: tick)
    first = db.process(match, live=True)
    tick += timedelta(hours=1)
    assert db.process(match, live=True) == first
    assert not list(db.history(player_id, prediction_date=day, as_of=tick))
    cut = day + timedelta(days=1)
    previous = list(db.history(player_id, prediction_date=cut, as_of=tick))
    assert metric(previous, "match_win")["numerator"] == 1
    assert previous[0]["availability_policy"] == "live_processed"

    unknown = input_match(canonical)
    with pytest.raises(ValueError, match="expected a different"):
        db.process(unknown, live=True)
    assert db.cached(unknown.match_id, unknown.input_revision) is None
    corrected = db.process(unknown, live=True, expected_previous=first)
    assert corrected != first
    assert not list(db.history(player_id, prediction_date=cut, as_of=tick + timedelta(seconds=1)))
    assert (
        metric(list(db.history(player_id, prediction_date=cut, as_of=tick)), "match_win")[
            "numerator"
        ]
        == 1
    )
    assert all(r["player_id"] is None for r in store.unpack(db.files(corrected)["match_sides"]))
    malformed = replace(match, input_revision="bad", tables={**match.tables, "matches": []})
    with pytest.raises(ValueError, match="one canonical match"):
        db.process(malformed, live=True, expected_previous=corrected)
    assert db.cached(match.match_id, "bad") is None


def test_history_dates_and_reproducible_incremental_snapshot(canonical, tmp_path):
    match, pid = verified_match(input_match(canonical))
    day = match.tables["matches"][0]["match_date"]
    another = str(UUID(int=355))
    clone = deepcopy(match.tables)
    for table in clone.values():
        for row in table:
            if "match_id" in row:
                row["match_id"] = another
    other = replace(match, match_id=another, input_revision="other", tables=clone)
    partial = Selection("base", match)
    full = Selection("next", match, other)
    first = build(partial, tmp_path / "incremental")
    updated = build(full, tmp_path / "incremental")
    rebuilt = build(full, tmp_path / "clean")
    assert first["version"] != updated["version"] == rebuilt["version"]
    assert updated["processed"] == updated["reused"] == 1
    assert updated["invalidated"] == [another]
    assert updated["earliest_affected_date"] == day.isoformat()
    assert updated["invalidated_player_ids"] == [pid]
    reader = SnapshotReader(tmp_path / "incremental")
    clean = SnapshotReader(tmp_path / "clean")
    assert hash_tree(reader.folder) == hash_tree(clean.folder)
    assert not reader.unique_target_day(pid, day)
    assert historical_before(day, day + timedelta(days=1)) and not historical_before(day, day)
    assert not list(reader.history(pid, prediction_date=day))
    assert (
        len(
            [
                r
                for r in reader.history(pid, prediction_date=day + timedelta(days=1))
                if r["metric"] == "match_win"
            ]
        )
        == 2
    )
    assert not list(
        reader.history(
            pid, prediction_date=day + timedelta(days=1), observed_cutoff=datetime.now(UTC)
        )
    )


def test_aggregation_counts_only_observed_points_and_completed_results(canonical):
    match = input_match(canonical)
    tables = deepcopy(match.tables)
    first = tables["match_player_pbp"][0]
    points = [
        {
            **first,
            "source_version": "fixture-pbp",
            "set_num": 1,
            "game_num": 1,
            "point_num": n,
            "winner_side": 1,
            "game_complete_verified": True,
        }
        for n in range(1, 5)
    ]
    tables["match_player_pbp"] = points
    rows = MatchAggregation(replace(match, tables=tables)).run()["metrics"]
    count = metric(rows, "observed_points_won", scope=1)
    assert count["quality"] == "VALID" and count["numerator"] == count["denominator"] == 4

    tables["match_player_pbp"] = points[:2] + points[-1:]
    tables["match_results"][0]["status"] = 9
    rows = MatchAggregation(replace(match, tables=tables)).run()["metrics"]
    count = metric(rows, "observed_points_won", scope=1)
    assert count["quality"] == "PARTIAL" and count["numerator"] == 3
    assert metric(rows, "match_win")["numerator"] is None
    assert not any(row["metric"] == "games_won" for row in rows)

    tables["match_player_pbp"].append(points[0])
    rows = MatchAggregation(replace(match, tables=tables)).run()["metrics"]
    assert metric(rows, "observed_points_won", scope=1)["quality"] == "CONFLICT"
