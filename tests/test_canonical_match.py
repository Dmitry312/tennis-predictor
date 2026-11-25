import copy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import uuid

import pytest

from tennis_predictor.ml.player_identity import PlayerRegistry
from tennis_predictor.ml.canonical_match import (
    ContractError, ScoreContract, Source, canonicalize, canonical_batch,
    winner_from_sets, prematch_context,
)

UTC = timezone.utc
T = datetime(2026, 1, 2, tzinfo=UTC)
C = ScoreContract("home_games", "away_games", 3, "tiebreak7", "synthetic-reviewed-mapping", "synthetic-v1")


def registry():
    r = PlayerRegistry()
    for n, name in [(1, "Synthetic One"), (2, "Synthetic Two")]:
        r.apply({"op": "create", "event_id": str(uuid.UUID(int=n)), "name": name, "gender": "M",
                 "provenance": {"source": "synthetic", "reference": "offline", "actor": "synthetic",
                                "observed_at": None, "recorded_at": "2025-01-01T00:00:00Z"}})
    return r


def scores(pairs):
    return json.dumps([{"home_games": a, "away_games": b} for a, b in pairs])


def input_row(sets=((6, 4), (6, 2)), status=3):
    # Actual writer function, preserving a synthetic explicitly mapped score shape.
    from tennis_predictor.ingestion.nbbet.details import MatchDetails, details_to_rows
    mid = str(uuid.UUID(int=100))
    d = MatchDetails(nbbet_match_id=100, status=status, has_coverage=True,
        sets=json.loads(scores(sets)), players={"p1": {"1": "Synthetic One"}, "p2": {"1": "Synthetic Two"}})
    details = details_to_rows(d, mid)[0]
    details["fetched_at"] = T+timedelta(hours=3)
    details["available_at"] = T+timedelta(hours=2)
    metadata = {"match_id": mid, "nbbet_match_id": 100, "duplicate_of": None, "gender": "M",
                "level": "tour", "player1": "Synthetic One", "player2": "Synthetic Two",
                "status": status, "start_ts": T.replace(tzinfo=None), "available_at": T-timedelta(days=1),
                "surface": "hard", "tournament_name": "Synthetic Tournament", "odds_open_p1": 2.2}
    return {"metadata": metadata, "details": details, "source": Source("synthetic-row", "a"*64), "score_contract": C}


def build(row=None, r=None):
    row = row or input_row()
    return canonicalize(row["metadata"], row["details"], r or registry(),
                        source=row["source"], score_contract=row.get("score_contract"))


@pytest.mark.parametrize("sets,y", [
    (((6, 0), (6, 0)), 1), (((4, 6), (2, 6)), 0),
    (((7, 6), (4, 6), (7, 5)), 1), (((6, 4), (1, 6), (4, 6)), 0)])
def test_bo3_targets(sets, y):
    r = build(input_row(sets))
    assert r.target.p1_win == y and r.target.reason == "validated_score"
    assert r.target.origin == "score_derived" and not r.target.independently_verified
    assert r.identity_provisional and not r.verified_training_eligible


def test_bo5_and_unknown_format():
    c = replace(C, best_of=5)
    assert winner_from_sets(scores([(6, 3), (6, 2), (6, 1)]), c) == 1
    with pytest.raises(ContractError):
        winner_from_sets(scores([(6, 3), (6, 2)]), c)
    with pytest.raises(ContractError):
        winner_from_sets(scores([(6, 3), (6, 2)]), replace(C, best_of=4))
    with pytest.raises(ContractError):
        winner_from_sets(scores([(6, 3), (6, 2)]), None)


@pytest.mark.parametrize("sets", [
    [(6, 5), (6, 2)], [(7, 4), (6, 2)], [(8, 6), (6, 2)],
    [(6, 6), (6, 2)], [(6, 3)], [(6, 3), (3, 6)], [(6, 3), (6, 2), (0, 6)],
    [(True, 4), (6, 2)], [("6", 4), (6, 2)], [(6.0, 4), (6, 2)], [(-6, 4), (6, 2)]])
def test_illegal_partial_and_extra_scores(sets):
    with pytest.raises(ContractError):
        winner_from_sets(scores(sets), C)


def test_documented_variants_require_explicit_contract():
    assert winner_from_sets(scores([(6, 4), (4, 6), (12, 10)]), replace(C, decider="advantage")) == 1
    with pytest.raises(ContractError):
        winner_from_sets(scores([(6, 4), (4, 6), (7, 6)]), replace(C, decider="advantage"))
    assert winner_from_sets(scores([(6, 4), (4, 6), (10, 8)]), replace(C, decider="match_tiebreak10")) == 1
    with pytest.raises(ContractError):
        winner_from_sets(scores([(6, 4), (4, 6), (11, 0)]), replace(C, decider="match_tiebreak10"))
    with pytest.raises(ContractError):
        winner_from_sets(scores([(6, 4), (4, 6), (6, 4)]), replace(C, decider="match_tiebreak10"))


def test_tiebreak_points_orientation_and_threshold():
    c = replace(C, p1_tiebreak_key="tb1", p2_tiebreak_key="tb2", decider="tiebreak10")
    sets = json.loads(scores([(6, 4), (4, 6), (7, 6)]))
    sets[-1].update(tb1=10, tb2=8)
    assert winner_from_sets(json.dumps(sets), c) == 1
    for a, b in [(7, 5), (8, 10), (12, 0), (True, 8)]:
        sets[-1].update(tb1=a, tb2=b)
        with pytest.raises(ContractError):
            winner_from_sets(json.dumps(sets), c)


@pytest.mark.parametrize("status", [1, 2, 4, 5, 6, 7, 8, 9, 10])
def test_status_exclusion(status):
    assert build(input_row(status=status)).target.p1_win is None


def test_orientation_flip_and_unknown():
    row = input_row()
    row["details"]["players_json"] = json.dumps({"p1": {"1": "Synthetic Two"}, "p2": {"1": "Synthetic One"}})
    record = build(row)
    assert record.orientation == "swapped" and record.target.p1_win == 0
    row["details"]["players_json"] = None
    assert build(row).target.p1_win is None
    assert build(row).quarantined


def test_identity_quarantine_same_player_and_snapshot_pin():
    row = input_row()
    r = registry()
    before = build(row, r)
    r.apply({"op": "create", "event_id": str(uuid.UUID(int=3)), "name": "Synthetic One", "gender": "M",
             "provenance": {"source": "synthetic", "reference": "offline", "actor": "synthetic",
                            "observed_at": None, "recorded_at": "2025-01-01T00:00:00Z"}})
    assert build(row, r).target.p1_win is None
    assert before.identity_digest != r.digest
    with pytest.raises(ContractError, match="identity_snapshot_mismatch"):
        prematch_context(before, r, cutoff=T)
    row["metadata"]["player2"] = "Synthetic One"
    assert build(row).target.p1_win is None


def test_unknown_timestamps_not_inferred_from_fetch_or_date():
    row = input_row()
    row["metadata"].update(start_ts=None, available_at=None, match_date="2026-01-02")
    row["details"].pop("available_at")
    result = build(row)
    assert result.event_time is None and result.metadata_available_at is None
    assert result.target.available_at is None and result.ingested_at is not None
    with pytest.raises(ContractError):
        prematch_context(result, registry(), cutoff=T)


@pytest.mark.parametrize("change,reason", [
    (lambda x: x["metadata"].update(level="itf"), "out_of_scope"),
    (lambda x: x["metadata"].update(duplicate_of=str(uuid.UUID(int=5))), "duplicate_excluded"),
    (lambda x: x["details"].update(canonical_match_id=str(uuid.UUID(int=5))), "match_join_conflict"),
    (lambda x: x["metadata"].update(player1="A/B"), "invalid_player_name"),
    (lambda x: x["metadata"].update(available_at=T.replace(tzinfo=None)), "ambiguous_timestamp")])
def test_reject_invalid_inputs(change, reason):
    row = input_row()
    change(row)
    with pytest.raises(ContractError, match=reason):
        build(row)


def test_contradictions_and_no_guessed_mapping():
    row = input_row()
    row["details"]["status"] = 9
    assert build(row).target.p1_win is None
    row = input_row()
    row["score_contract"] = None
    assert build(row).target.reason == "unknown_score_contract"
    row = input_row()
    row["details"]["available_at"] = T-timedelta(days=2)
    assert build(row).target.p1_win is None
    row = input_row()
    row["details"]["nbbet_match_id"] = 777
    assert build(row).target.p1_win is None


def test_dedup_correction_ties_and_unknown_order():
    row = input_row()
    r = registry()
    batch = canonical_batch([row, copy.deepcopy(row)], r)
    assert len(batch["records"]) == 1
    assert batch["counts"]["deduplicated_or_superseded"] == 1
    correction = input_row(((1, 6), (2, 6)))
    correction["details"]["fetched_at"] += timedelta(hours=1)
    correction["source"] = Source("correction", "b"*64)
    for items in ([row, correction], [correction, row]):
        out = canonical_batch(items, r)
        assert len(out["records"]) == 1 and out["records"][0].target.p1_win == 0
    correction["details"]["fetched_at"] = row["details"]["fetched_at"]
    assert canonical_batch([row, correction], r)["counts"]["correction_conflict"] == 1
    correction["details"]["fetched_at"] = None
    assert canonical_batch([row, correction], r)["counts"]["unordered_correction"] == 1


def test_asof_corrections_unknown_availability_and_source_collisions():
    row = input_row()
    later = input_row(((2, 6), (1, 6)))
    later["details"]["available_at"] = T+timedelta(hours=5)
    later["details"]["fetched_at"] = T+timedelta(hours=6)
    out = canonical_batch([row, later], registry(), as_of=T+timedelta(hours=4))
    assert out["records"][0].target.p1_win == 1
    assert out["counts"]["result_after_cutoff"] == 1
    row["details"].pop("available_at")
    assert not canonical_batch([row], registry(), as_of=T+timedelta(hours=4))["records"]
    other = input_row()
    other["metadata"]["match_id"] = other["details"]["canonical_match_id"] = str(uuid.UUID(int=101))
    assert canonical_batch([input_row(), other], registry())["counts"]["source_id_collision"] == 2


def test_feature_projection_has_no_outcome_or_odds_leakage():
    r = registry()
    row = input_row()
    first = prematch_context(build(row, r), r, cutoff=T)
    row["metadata"].update(odds_open_p1=999, odds_open_p2=1.1)
    row["details"].update(sets_json=scores([(1, 6), (2, 6)]), p1_rank=1)
    second = prematch_context(build(row, r), r, cutoff=T)
    assert first == second
    assert not ({"status", "target", "p1_win", "sets_json", "odds_open_p1", "p1_rank"} & first.keys())
    row["metadata"]["available_at"] = T+timedelta(hours=1)
    with pytest.raises(ContractError):
        prematch_context(build(row, r), r, cutoff=T)


def test_future_identity_snapshot_and_batch_limit():
    r = registry()
    r.apply({"op": "alias", "event_id": str(uuid.UUID(int=3)), "name": "New Alias",
             "player_id": r.resolve("M", "Synthetic One").player_id,
             "provenance": {"source": "synthetic", "reference": "offline", "actor": "synthetic",
                            "observed_at": None, "recorded_at": "2027-01-01T00:00:00Z"}})
    with pytest.raises(ContractError, match="identity_after_cutoff"):
        prematch_context(build(input_row(), r), r, cutoff=T)
    with pytest.raises(ContractError, match="input_limit"):
        canonical_batch([input_row(), input_row()], registry(), limit=1)


def test_validated_score_orientation_units_and_same_winner_conflicts():
    row = input_row()
    result = build(row)
    assert [(s.p1, s.p2, s.unit) for s in result.validated_sets] == [(6, 4, "games"), (6, 2, "games")]
    row["details"]["players_json"] = json.dumps({"p1": {"1": "Synthetic Two"}, "p2": {"1": "Synthetic One"}})
    assert [(s.p1, s.p2) for s in build(row).validated_sets] == [(4, 6), (2, 6)]
    same_winner = input_row(((6, 1), (6, 0)))
    out = canonical_batch([input_row(), same_winner], registry())
    assert not out["records"] and out["counts"]["correction_conflict"] == 1
    row = input_row(((6, 4), (4, 6), (10, 0)))
    row["score_contract"] = replace(C, decider="match_tiebreak10")
    assert build(row).validated_sets[-1].unit == "points"


def test_metadata_asof_and_same_day_retrospective_identity_are_not_assumed():
    row = input_row()
    row["metadata"]["available_at"] = None
    out = canonical_batch([row], registry(), as_of=T+timedelta(hours=4))
    assert out["counts"]["unknown_metadata_availability"] == 1
    row["metadata"]["available_at"] = T+timedelta(days=1)
    assert canonical_batch([row], registry(), as_of=T+timedelta(hours=4))["counts"]["metadata_after_cutoff"] == 1


def test_malformed_json_and_unknown_contract_cannot_create_target():
    row = input_row()
    row["details"]["sets_json"] = '[{"home_games":6,"home_games":1,"away_games":4}]'
    assert build(row).target.p1_win is None
    row["details"]["sets_json"] = "not-json"
    assert build(row).target.p1_win is None
    row["score_contract"] = "unknown"
    with pytest.raises(ContractError):
        build(row)


def test_invalid_correction_never_falls_back_to_older_valid_label():
    row = input_row()
    broken = input_row()
    broken["metadata"]["start_ts"] = "not-a-timestamp"
    broken["details"]["fetched_at"] += timedelta(hours=1)
    out = canonical_batch([row, broken], registry())
    assert not out["records"]
    assert out["counts"]["invalid_timestamp"] == 1
    assert out["counts"]["invalid_correction_quarantine"] == 1
