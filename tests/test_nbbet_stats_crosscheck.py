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


def test_reconcile_dry_run_never_writes(monkeypatch):
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
    assert result["changed"] == 1
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
