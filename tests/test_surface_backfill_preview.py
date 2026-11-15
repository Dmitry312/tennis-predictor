"""Offline tests for the tournament-surface preview decision policy."""
from tennis_predictor.ingestion.nbbet.surface_backfill_preview import decide


def test_known_surface_is_reused():
    assert decide(["hard"], [], 2) == ("known_consistent", "hard")


def test_two_probes_agree():
    assert decide([], ["clay", "clay"], 2) == ("probed_consistent", "clay")


def test_one_probe_is_not_enough():
    assert decide([], ["grass", None], 2) == ("insufficient_evidence", None)


def test_conflicting_probes_are_not_approved():
    assert decide([], ["clay", "hard"], 2) == ("conflict_probe", None)


def test_conflicting_known_values_are_not_approved():
    assert decide(["hard", "grass"], [], 2) == ("conflict_existing", None)


def test_unknown_is_not_persistable():
    assert decide([], ["unknown", "hard"], 2) == ("invalid_probe", None)
