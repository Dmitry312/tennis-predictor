"""Offline policy checks for year-scoped surface planning."""
import pytest

from tennis_predictor.ingestion.nbbet.surface_backfill_year_plan import (
    plan, requires_tie_level, decide,
)


@pytest.mark.parametrize("name", [
    "Davis Cup. World Group", "Кубок Дэвиса. Группа 3",
    "DAVIS CUP Finals",
])
def test_davis_cup_requires_tie_level(name):
    assert requires_tie_level(name)


@pytest.mark.parametrize("name", [
    "ATP Madrid", "WTA Australian Open", "ATP Challenger. Rome",
])
def test_regular_tournaments_are_year_scoped_not_tie_scoped(name):
    assert not requires_tie_level(name)


@pytest.mark.parametrize("limit,pause", [(0, 3), (21, 3), (5, 0)])
def test_invalid_limits_fail_before_db(limit, pause):
    with pytest.raises(ValueError):
        plan(limit, False, pause)


@pytest.mark.parametrize("known,probes,expected", [
    (["hard"], [], ("known_consistent", "hard")),
    ([], ["clay", "clay"], ("probed_consistent", "clay")),
    ([], ["grass", None], ("insufficient_evidence", None)),
    ([], ["hard", "clay"], ("conflict_probe", None)),
    (["hard", "clay"], [], ("conflict_existing", None)),
    ([], ["unknown", "hard"], ("invalid_probe", None)),
])
def test_remote_standalone_decision_policy(known, probes, expected):
    assert decide(known, probes, 2) == expected
