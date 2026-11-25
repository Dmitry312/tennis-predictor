import json

from tennis_predictor.ingestion.nbbet.identity_audit import audit


def test_candidate_repetition_and_redaction():
    raw = json.dumps({"p1": {"1": 12345, "2": "Private Name"}, "p2": {"1": 67890}})
    report = audit([{"players_json": raw}] * 2)
    assert report["counts"]["player_objects"] == 4
    candidate = next(p for p in report["candidate_paths"] if p["path"] == "1")
    assert candidate["distinct"] == 2
    assert candidate["repeated_values"] == 2
    assert "Private Name" not in json.dumps(report)
    assert "12345" not in json.dumps(report)


def test_invalid_and_limit():
    report = audit([{"players_json": "bad"}, {"players_json": None}, {}], limit=2)
    assert report["counts"] == {"rows": 2, "invalid_json": 1, "missing_or_oversized": 1}
