"""Offline checks for year-scoped surface backfill safety rules."""
import pytest

from tennis_predictor.ingestion.nbbet.surface_backfill_year_write import (
    blocked, eligible, run,
)


@pytest.mark.parametrize("name", ["Davis Cup World Group", "Кубок Дэвиса. Группа 3"])
def test_davis_cup_is_excluded(name):
    assert blocked(name)


def test_two_matching_probes_are_required():
    assert eligible(0, None, 0, ["hard", "hard"]) == "hard"
    assert eligible(0, None, 0, ["hard", None]) is None
    assert eligible(0, None, 0, ["hard", "clay"]) is None
    assert eligible(0, None, 0, ["unknown", "hard"]) is None


def test_existing_surface_must_match_probes():
    assert eligible(1, "clay", 0, ["clay", "clay"]) == "clay"
    assert eligible(1, "hard", 0, ["clay", "clay"]) is None
    assert eligible(2, "clay", 0, ["clay", "clay"]) is None
    assert eligible(0, None, 1, ["clay", "clay"]) is None


@pytest.mark.parametrize("year,pause", [(2000, 3), (2025, 1)])
def test_invalid_args_fail_before_db(year, pause):
    with pytest.raises(ValueError):
        run(year, False, pause)
