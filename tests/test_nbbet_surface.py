"""Regression tests for NB-Bet surface extraction from match pages."""
import json

import pytest

from tennis_predictor.ingestion.nbbet.details import parse_match_page, parse_surface


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("WTA. Finals - Indian Wells - хард, Финал", "hard"),
        ("ATP - hard, Quarterfinal", "hard"),
        ("ATP - грунт", "clay"),
        ("ATP - clay", "clay"),
        ("WTA - трава", "grass"),
        ("WTA - grass", "grass"),
        ("WTA - хард, грунт", None),
        ("ATP - hardwood", None),
        ("ATP - без указания покрытия", None),
        ("", None),
        (None, None),
        (123, None),
    ],
)
def test_parse_surface(text, expected):
    assert parse_surface(text) == expected


@pytest.mark.parametrize(
    ("match_one", "expected"),
    [
        ({"1": "WTA. Finals - Indian Wells - хард, Финал"}, "hard"),
        ({"1": "ATP. Rome - грунт"}, "clay"),
        ({"1": "ATP. Wimbledon - трава"}, "grass"),
        ({"1": "ATP. Rome"}, None),
        ({"1": "ATP - hard / clay"}, None),
        ({}, None),
        (None, None),
        ("unexpected", None),
    ],
)
def test_parse_match_page_surface(match_one, expected):
    state = {"props": {"initialState": {"pageTennisEvent": {
        "match": {"1": match_one, "23": 3, "24": [], "25": [], "27": False}
    }}}}
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(state) + "</script>"
    result = parse_match_page(html, 123)
    assert result.surface == expected


def test_process_one_keeps_surface_when_points_are_deferred(monkeypatch):
    import threading
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from tennis_predictor.ingestion.nbbet import detail_job
    from tennis_predictor.ingestion.nbbet.details import MatchDetails

    match = SimpleNamespace(
        nbbet_slug="sample-slug", nbbet_match_id=123,
        match_id="sample-match", has_pbp=False, match_date=datetime.now(UTC).date(),
    )
    monkeypatch.setattr(detail_job, "_session", lambda guard: object())
    monkeypatch.setattr(detail_job, "fetch_match_page", lambda session, slug: "html")
    monkeypatch.setattr(detail_job, "parse_match_page", lambda html, mid: MatchDetails(
        nbbet_match_id=mid, status=3, has_coverage=False, surface="hard",
    ))
    monkeypatch.setattr(detail_job, "fetch_points", lambda session, slug: None)
    monkeypatch.setattr(detail_job.time, "sleep", lambda seconds: None)
    result = detail_job.process_one(match, detail_job.ThrottleGuard(), {"pause": 0}, threading.Event())
    assert result.kind == "retry_later"
    assert result.surface == "hard"


def test_process_one_keeps_surface_when_status_changed(monkeypatch):
    import threading
    from types import SimpleNamespace

    from tennis_predictor.ingestion.nbbet import detail_job
    from tennis_predictor.ingestion.nbbet.details import MatchDetails

    match = SimpleNamespace(nbbet_slug="sample-slug", nbbet_match_id=123)
    monkeypatch.setattr(detail_job, "_session", lambda guard: object())
    monkeypatch.setattr(detail_job, "fetch_match_page", lambda session, slug: "html")
    monkeypatch.setattr(detail_job, "parse_match_page", lambda html, mid: MatchDetails(
        nbbet_match_id=mid, status=5, has_coverage=False, surface="clay",
    ))
    monkeypatch.setattr(detail_job.time, "sleep", lambda seconds: None)
    result = detail_job.process_one(match, detail_job.ThrottleGuard(), {"pause": 0}, threading.Event())
    assert result.kind == "status_changed"
    assert result.surface == "clay"
