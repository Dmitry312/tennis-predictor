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
