"""Offline tests for full-field NBBet statistics reconciliation."""
import pytest
from tennis_predictor.ingestion.nbbet.stats_crosscheck import (
    FIELDS, latest_rows, stat_differences,
)


def row(scope, **overrides):
    return {"scope": scope, **{field: None for field in FIELDS}, **overrides}


def test_identical_stats_all_scopes():
    old = [row(0, aces_p1=4, first_serve_points_all_p2=22),
           row(1, first_serve_points_all_p2=12),
           row(2, first_serve_points_all_p2=10)]
    assert stat_differences(old, old) == []


def test_detects_actual_geerts_second_set_correction():
    old = [row(0, first_serve_points_all_p2=22),
           row(1, first_serve_points_all_p2=12),
           row(2, first_serve_points_all_p2=10)]
    new = [row(0, first_serve_points_all_p2=21),
           row(1, first_serve_points_all_p2=12),
           row(2, first_serve_points_all_p2=9)]
    assert stat_differences(old, new) == [
        (0, "first_serve_points_all_p2", 22.0, 21.0),
        (2, "first_serve_points_all_p2", 10.0, 9.0),
    ]


def test_checks_all_28_stat_fields():
    assert len(FIELDS) == 28
    for field in FIELDS:
        assert stat_differences([row(0, **{field: 1})],
                                [row(0, **{field: 2})]) == [(0, field, 1.0, 2.0)]


def test_rejects_source_missing_stat_and_scope():
    with pytest.raises(ValueError, match="removed"):
        stat_differences([row(0, aces_p1=2)], [row(0)])
    with pytest.raises(ValueError, match="scopes"):
        stat_differences([row(0), row(1)], [row(0)])
    with pytest.raises(ValueError, match="duplicate"):
        stat_differences([row(0)], [row(0), row(0)])


def test_newest_parquet_row_wins():
    rows = [
        {"canonical_match_id": "abc", "scope": 2, "fetched_at": "2026-11-23 12:20:20", "aces_p1": 1},
        {"canonical_match_id": "abc", "scope": 2, "fetched_at": "2026-11-24 10:20:20", "aces_p1": 2},
        {"canonical_match_id": "abc", "scope": 1, "fetched_at": "2026-11-23 12:20:20", "aces_p1": 3},
    ]
    selected = latest_rows(rows)
    assert selected[("abc", 2)][1]["aces_p1"] == 2
    assert selected[("abc", 1)][1]["aces_p1"] == 3


def test_reconcile_dry_run_never_writes(monkeypatch, capsys):
    import time
    from datetime import date
    from types import SimpleNamespace
    from tennis_predictor.ingestion.nbbet import stats_crosscheck as mod

    match = SimpleNamespace(match_id="abc", nbbet_match_id=123,
                            nbbet_slug="123-slug", gender="M", level="challenger",
                            match_date=date(2026, 11, 23), pbp_s3_path="pbp/example")
    stored = {("abc", 0): ("2026-11-23", row(0, first_serve_points_all_p2=10,
                                              canonical_match_id="abc"))}
    new = SimpleNamespace(stats=[row(0, first_serve_points_all_p2=9)],
                          raw_page_tennis_event={"match": {"25": []}})
    writes = []
    monkeypatch.setattr(mod, "fetch_candidates", lambda *args: [match])
    monkeypatch.setattr(mod, "get_s3_client", lambda: SimpleNamespace(put_object=lambda **k: writes.append(k)))
    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(s3_bucket="test"))
    monkeypatch.setattr(mod, "make_session", lambda **kw: object())
    monkeypatch.setattr(mod, "load_group", lambda *args: stored)
    monkeypatch.setattr(mod, "fetch_match_page", lambda *args: "<html/>")
    monkeypatch.setattr(mod, "parse_match_page", lambda *args: new)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    result = mod.reconcile(day=date(2026, 11, 23), lookback=1, apply=False,
                           max_matches=100, pause=0.5, deadline=time.monotonic() + 100)
    import json
    assert result["changed"] == 1
    assert json.loads(capsys.readouterr().out) == result
    assert result["written"] == 0
    assert writes == []


def test_reconcile_does_not_publish_partial_results(monkeypatch):
    import time
    from datetime import date
    from types import SimpleNamespace
    from tennis_predictor.ingestion.nbbet import stats_crosscheck as mod

    match = SimpleNamespace(match_id="abc", nbbet_match_id=123,
                            nbbet_slug="123-slug", gender="M", level="challenger",
                            match_date=date(2026, 11, 23), pbp_s3_path="pbp/example")
    writes = []
    monkeypatch.setattr(mod, "fetch_candidates", lambda *args: [match])
    monkeypatch.setattr(mod, "get_s3_client", lambda: SimpleNamespace(put_object=lambda **k: writes.append(k)))
    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(s3_bucket="test"))
    monkeypatch.setattr(mod, "make_session", lambda **kw: object())
    monkeypatch.setattr(mod, "load_group", lambda *args: {
        ("abc", 0): ("2026-11-23", row(0, aces_p1=3, canonical_match_id="abc"))})
    monkeypatch.setattr(mod, "fetch_match_page", lambda *args: "<html/>")
    monkeypatch.setattr(mod, "parse_match_page", lambda *args: SimpleNamespace(
        stats=[row(0, aces_p1=None)]))
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="incomplete"):
        mod.reconcile(day=date(2026, 11, 23), lookback=1, apply=True,
                      max_matches=100, pause=0.5, deadline=time.monotonic() + 100)
    assert writes == []


def test_reconcile_apply_writes_raw_and_stats_versions(monkeypatch):
    import time
    from datetime import date
    from types import SimpleNamespace
    from tennis_predictor.ingestion.nbbet import stats_crosscheck as mod

    match = SimpleNamespace(match_id="abc", nbbet_match_id=123,
                            nbbet_slug="123-slug", gender="M", level="challenger",
                            match_date=date(2026, 11, 23), pbp_s3_path="pbp/example")
    stored = {("abc", 0): ("2026-11-23", row(0, aces_p1=1, canonical_match_id="abc"))}
    parsed = SimpleNamespace(stats=[row(0, aces_p1=2)],
                             raw_page_tennis_event={"match": {"25": []}})
    writes = []
    monkeypatch.setattr(mod, "fetch_candidates", lambda *args: [match])
    monkeypatch.setattr(mod, "get_s3_client", lambda: SimpleNamespace(
        put_object=lambda **k: writes.append(k)))
    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(s3_bucket="test"))
    monkeypatch.setattr(mod, "make_session", lambda **kw: object())
    monkeypatch.setattr(mod, "load_group", lambda *args: stored)
    monkeypatch.setattr(mod, "fetch_match_page", lambda *args: "<html/>")
    monkeypatch.setattr(mod, "parse_match_page", lambda *args: parsed)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    result = mod.reconcile(day=date(2026, 11, 23), lookback=1, apply=True,
                           max_matches=100, pause=0.5, deadline=time.monotonic() + 100)
    assert result["changed"] == 1
    assert result["written"] == 1
    assert len(writes) == 2
    assert writes[0]["Key"].startswith("nbbet_raw/gender=M/level=challenger/year=2026/")
    assert writes[1]["Key"].startswith("nbbet_stats/gender=M/level=challenger/year=2026/")
    assert all(w["IfNoneMatch"] == "*" for w in writes)


def test_changed_counter_validation():
    from tennis_predictor.ingestion.nbbet.stats_crosscheck import validate_changed_stats
    field = "first_serve_points_all_p2"
    changes = [(0, field, 22, 21)]
    validate_changed_stats([row(0, **{field: 21}), row(1, **{field: 12}),
                            row(2, **{field: 9})], changes)
    with pytest.raises(ValueError, match="mismatch"):
        validate_changed_stats([row(0, **{field: 22}), row(1, **{field: 12}),
                                row(2, **{field: 9})], changes)
    with pytest.raises(ValueError, match="invalid counter"):
        validate_changed_stats([row(0, **{field: -1})], changes)


def test_backfill_resume(tmp_path, monkeypatch):
    from datetime import date
    from tennis_predictor.ingestion.nbbet import stats_crosscheck_backfill as mod
    calls = []
    def reconcile(**kwargs):
        calls.append((kwargs["day"], kwargs["apply"]))
        return {"checked": 1}
    monkeypatch.setattr(mod, "reconcile", reconcile)
    start, end = date(2025, 1, 1), date(2025, 1, 2)
    assert not mod.run(start, end, tmp_path, max_days=1)
    assert mod.run(start, end, tmp_path, max_days=1)
    assert mod.run(start, end, tmp_path)
    assert calls == [(start, False), (end, False)]
    assert mod.run(start, end, tmp_path, apply=True)
    assert calls[-2:] == [(start, True), (end, True)]


def test_daily_status(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from tennis_predictor.ingestion.nbbet import stats_crosscheck_daily as mod
    from tennis_predictor.monitoring.daily_telegram_report import crosscheck_report
    day = datetime.now(mod.TZ).date() - timedelta(days=1)
    monkeypatch.setattr(mod, "reconcile", lambda **kw: {
        "checked": 10, "changed": 2, "fields_changed": 4, "written": 0})
    mod.run(tmp_path)
    assert "10 проверено" in crosscheck_report(day, tmp_path)
    def fail(**kw):
        raise RuntimeError("failure")
    monkeypatch.setattr(mod, "reconcile", fail)
    with pytest.raises(RuntimeError):
        mod.run(tmp_path)
    assert "error" in crosscheck_report(day, tmp_path)


def test_reverse_backfill(tmp_path, monkeypatch):
    from datetime import date
    from tennis_predictor.ingestion.nbbet import stats_crosscheck_backfill as mod
    calls = []
    def reconcile(**kw):
        calls.append(kw["day"])
        return {"checked": 1}
    monkeypatch.setattr(mod, "reconcile", reconcile)
    start, end = date(2025, 1, 1), date(2025, 1, 3)
    assert not mod.run(start, end, tmp_path, max_days=1, reverse=True)
    assert mod.run(start, end, tmp_path, max_days=2, reverse=True)
    assert calls == [end, date(2025, 1, 2), start]
