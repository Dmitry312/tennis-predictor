"""Bulk owner policy and pinned canonical/store integration."""

from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tennis_predictor.ml.aggregation.batch import CanonicalReader, SnapshotReader
from tennis_predictor.ml.aggregation.batch import build as build_matches
from tennis_predictor.ml.aggregation.integration import live_match_input
from tennis_predictor.ml.aggregation.store import MatchDataStore
from tennis_predictor.ml.canonical_layer.identity import PinnedIdentities
from tennis_predictor.ml.canonical_layer.source import CatalogAdapter
from tennis_predictor.ml.canonical_layer.storage import build as build_canonical
from tennis_predictor.ml.identity_review import staging
from tennis_predictor.ml.identity_review.bulk import import_approved_groups
from tennis_predictor.ml.identity_review.registry import ReviewedMappings
from tests.test_identity_review import Source, match, observed

ROOT = Path(__file__).resolve().parents[1]
RECORDED = "2026-11-27T10:00:00Z"
CFG = {"version": "canonical-v1", "mode": "strict_as_of", "reviews": {}}


def packet(dataset, groups, parent="root"):
    return {
        "dataset": dataset,
        "groups": groups,
        "parent": parent,
        "approval_ref": "fixture owner approval",
        "recorded_at": RECORDED,
    }


def test_bulk_groups_keep_ids_and_report_overlap(tmp_path):
    stage, registry = tmp_path / "stage.sqlite", tmp_path / "registry.sqlite"
    rows = [match(1), match(2, "Ivanov I."), match(3, "Ivanov I.", gender="F")]
    dataset = staging.extract(stage, Source(rows))["dataset"]
    male, female = (observed(stage, n, 1)["group_id"] for n in (1, 3))
    assert male == observed(stage, 2, 1)["group_id"] and male != female
    approval = packet(dataset, {male: "male-1", female: "female-1"})
    result = import_approved_groups(stage, registry, approval)
    assert result["approved_groups"] == 2 and result["aliases"] == 3
    assert import_approved_groups(stage, registry, approval) == result
    with closing(staging.connection(registry)) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    reviewed = ReviewedMappings(registry)
    first, second, third = (reviewed.resolve_match(row, source="nbbet")[0] for row in rows)
    assert first["player_id"] == second["player_id"] != third["player_id"]
    assert first["match_quality_flags"] == ["same_start_distinct_matches"]
    assert first["provenance"] == "bulk_owner_policy"
    new = match(4, "Ivanov I.", start_ts=None)
    assert reviewed.resolve_match(new, source="nbbet")[0]["player_id"] == first["player_id"]
    bad = packet(dataset, {male: "same", female: "same"}, result["token"])
    with pytest.raises(ValueError, match="gender"):
        import_approved_groups(stage, registry, bad)
    narrowed = import_approved_groups(
        stage, registry, packet(dataset, {male: "male-1"}, result["token"])
    )
    assert (
        reviewed.resolve_match(new, source="nbbet")[0]["evidence_token"] == first["evidence_token"]
    )
    assert reviewed.resolve_match(rows[2], source="nbbet")[0]["status"] == "quarantine"
    assert (
        reviewed.resolve_match(rows[2], source="nbbet", revision=1)[0]["player_id"]
        == third["player_id"]
    )
    assert list(reviewed.alias_invalidations(narrowed["revision"])) == [
        {"source": "nbbet", "gender": "F", "normalized": "ivanov i."}
    ]
    opposing = {**new, "player2": "Иванов И."}
    assert all(
        side["reason"] == "opposing_sides"
        for side in reviewed.resolve_match(opposing, source="nbbet")
    )


def test_pinned_catalog_and_live_store_with_incremental_correction(tmp_path, monkeypatch):
    catalog = ROOT / "docs/ml/data_catalog"
    stage, registry = tmp_path / "stage.sqlite", tmp_path / "registry.sqlite"
    dataset = staging.extract(stage, staging.CatalogSource(catalog))["dataset"]
    with closing(staging.connection(stage)) as db:
        groups = {
            row[0]: f"fixture-{i}"
            for i, row in enumerate(
                db.execute("SELECT DISTINCT group_id FROM observations ORDER BY group_id")
            )
        }
    first = import_approved_groups(stage, registry, packet(dataset, groups))
    identities = PinnedIdentities(registry, 1)
    adapter = CatalogAdapter(catalog)
    output = tmp_path / "canonical"
    report = build_canonical(adapter, output, CFG, identities=identities)
    assert report["processed_matches"] == 20
    reader = CanonicalReader(output / CFG["mode"], report["version"])
    match_id = "00035326-4450-5d09-b060-86a1f6851e63"
    item = reader.load(match_id, dict(reader.entries())[match_id])
    player = item.tables["players"][0]
    assert player["resolution_status"] == "verified"
    assert player["identity_provenance"] == "bulk_owner_policy"
    assert reader.manifest["inputs"]["registry_token"] == first["token"]
    built = build_matches(reader, tmp_path / "match-data")
    assert built["match_sides"] == 40
    history = SnapshotReader(tmp_path / "match-data")
    assert list(history.history(player["player_id"], prediction_date=date(2030, 1, 1)))
    assert not list(history.history(player["player_id"], prediction_date=date(2000, 1, 1)))

    bundle = adapter.load(match_id, dict(adapter.entries())[match_id])
    live = live_match_input(bundle, CFG, identities)
    from tennis_predictor.ml.aggregation import store

    monkeypatch.setattr(store, "clock", lambda: datetime(2026, 11, 28, tzinfo=UTC))
    sqlite = MatchDataStore(tmp_path / "live.sqlite")
    rev = sqlite.process(live, live=True)
    assert sqlite.process(live, live=True) == rev
    assert list(
        sqlite.history(
            player["player_id"],
            prediction_date=date(2030, 1, 1),
            as_of=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )

    with closing(staging.connection(stage)) as db:
        dropped = db.execute(
            "SELECT group_id FROM observations WHERE match_id=? AND side=1", (match_id,)
        ).fetchone()[0]
    groups.pop(dropped)
    import_approved_groups(stage, registry, packet(dataset, groups, first["token"]))
    second = build_canonical(adapter, output, CFG, identities=PinnedIdentities(registry, 2))
    assert second["changes"]["changed"] == 1 and second["changes"]["skipped"] == 19
    assert player["player_id"] in second["invalidation"]["player_ids"]
    corrected = build_matches(
        CanonicalReader(output / CFG["mode"], second["version"]), tmp_path / "match-data"
    )
    assert corrected["invalidated"] == [match_id]
    assert not list(
        SnapshotReader(tmp_path / "match-data").history(
            player["player_id"], prediction_date=date(2030, 1, 1)
        )
    )
