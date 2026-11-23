"""Offline regression for exact raw NBBet stat indices."""
import importlib.util
import json
from pathlib import Path


def test_raw_stat_probe_reads_original_second_set_denominator():
    path = Path(__file__).resolve().parents[1] / "infra/audit/production_raw_stat_probe.py"
    spec = importlib.util.spec_from_file_location("raw_stat_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def row(won, total):
        cells = [None] * 15
        cells[3] = [28, total]
        cells[4] = [25, won]
        return cells
    raw = json.dumps({"match": {"25": [row(10, 21), row(4, 12), row(6, 9)]}})
    assert module.extract_scopes(raw) == [
        {"scope": 0, "first_serve_all_p2": 21, "first_serve_won_p2": 10},
        {"scope": 1, "first_serve_all_p2": 12, "first_serve_won_p2": 4},
        {"scope": 2, "first_serve_all_p2": 9, "first_serve_won_p2": 6},
    ]
