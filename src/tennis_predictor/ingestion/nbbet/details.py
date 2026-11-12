"""Страница матча nb-bet: статус, счёт по сетам, статистика, рейтинг.

Данные лежат в серверном HTML:
  __NEXT_DATA__ -> props.initialState.pageTennisEvent.match
    "3"/"5"  игрок 1/2: {"1": имя, "2": фото, "3": bool, "4": {"1": ранг, "2": "ATP"/"WTA"}}
             ранг — на дату МАТЧА, не текущий (проверено: тот же игрок
             даёт разные значения на страницах разных по времени
             матчей, см. docs/decisions.md). "points" (очки рейтинга),
             возраст, рост, рука — здесь не найдены ни на одном
             уровне (tour/challenger/ITF, 2018-2026).
    "23"  статус (enum status.py)
    "24"  список сетов: счёт и длительность
    "25"  статистика: строка 0 = матч, дальше по сетам; 15 элементов,
          индекс 1..14 = показатель, значение [игрок1, игрок2] или None
    "27"  флаг покрытия (live-трекинг), совпадает с наличием "25"
    "32"  корт и погода
"""

import json
import re
from dataclasses import dataclass, field

from tennis_predictor.ingestion.nbbet.odds import odds_pair

PAGE_URL = "https://nb-bet.com/tennis/events/{slug}"
PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru",
    "Referer": "https://nb-bet.com/",
}

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)

STAT_FIELDS = {
    1: "aces",
    2: "double_faults",
    3: "first_serve_points_all",
    4: "first_serve_points_won",
    5: "second_serve_points_all",
    6: "second_serve_points_won",
    7: "break_points_won",       # на tour/challenger всегда пусто (60 из 60)
    8: "break_points_all",       # но храним: перекачка страниц слишком дорога
    9: "winners",
    10: "unforced_errors",
    11: "max_points_in_row",
    12: "max_games_in_row",
    13: "service_games_won",
    14: "service_games_all",
}


class PageParseError(Exception):
    pass


# 2 = + players_json/p1_rank/p2_rank/p1_rank_tour/p2_rank_tour
# 3 = + сырой pageTennisEvent целиком (nbbet_raw/...) — поля, которые
# мы ещё не разобрали (например "10"-"13", коэффициенты "30"/"31"),
# не теряются при каждом новом разборе; перекачка ради них не нужна.
DETAILS_VERSION = 3


@dataclass
class MatchDetails:
    nbbet_match_id: int
    status: int | None
    has_coverage: bool | None
    sets: list = field(default_factory=list)
    stats: list[dict] = field(default_factory=list)  # по строке на scope
    extra: dict | None = None
    players: dict | None = None  # {"p1": match["3"], "p2": match["5"]}
    raw_page_tennis_event: dict | None = None  # props.initialState.pageTennisEvent целиком
    odds_open: tuple[float, float] | None = None  # odds_pair(match["30"]) — запасной путь, см. sweep.py
    surface: str | None = None


_SURFACE_MARKERS = {
    "hard": {"hard", "хард"},
    "clay": {"clay", "грунт"},
    "grass": {"grass", "трава"},
}


def parse_surface(value: object) -> str | None:
    """Return a surface only for a single unambiguous standalone marker."""
    if not isinstance(value, str):
        return None
    tokens = set(re.findall(r"[^\W\d_]+", value.casefold(), flags=re.UNICODE))
    matches = [surface for surface, markers in _SURFACE_MARKERS.items() if tokens & markers]
    return matches[0] if len(matches) == 1 else None


def fetch_match_page(session, slug: str) -> str | None:
    """None если 404 (страницы нет). Остальные ошибки пробрасываются."""
    resp = session.get(PAGE_URL.format(slug=slug), headers=PAGE_HEADERS, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def parse_match_page(html: str, nbbet_match_id: int) -> MatchDetails:
    found = _NEXT_DATA.search(html)
    if not found:
        raise PageParseError("нет __NEXT_DATA__")
    try:
        page_tennis_event = json.loads(found.group(1))["props"]["initialState"]["pageTennisEvent"]
        match = page_tennis_event["match"]
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        raise PageParseError(f"неожиданная структура: {e!r}") from e

    stats = []
    for scope, row in enumerate(match.get("25") or []):
        rec = {"scope": scope}  # 0 = матч целиком, 1.. = номер сета
        for code, name in STAT_FIELDS.items():
            pair = row[code] if isinstance(row, list) and len(row) > code else None
            ok = isinstance(pair, (list, tuple)) and len(pair) == 2
            rec[f"{name}_p1"] = pair[0] if ok else None
            rec[f"{name}_p2"] = pair[1] if ok else None
        stats.append(rec)

    return MatchDetails(
        nbbet_match_id=nbbet_match_id,
        status=match.get("23"),
        has_coverage=match.get("27"),
        sets=match.get("24") or [],
        stats=stats,
        extra=match.get("32"),
        players={"p1": match.get("3"), "p2": match.get("5")},
        raw_page_tennis_event=page_tennis_event,
        odds_open=odds_pair(match.get("30")),
        surface=parse_surface((match.get("1") or {}).get("1") if isinstance(match.get("1"), dict) else None),
    )


def _rank(player: dict | None) -> tuple[int | None, str | None]:
    rank = (player or {}).get("4") or {}
    return rank.get("1"), rank.get("2")


def details_to_rows(d: MatchDetails, canonical_match_id: str) -> tuple[dict, list[dict], dict]:
    """(строка деталей матча, строки статистики по scope, строка сырого
    pageTennisEvent) для трёх Parquet-наборов (nbbet_details/nbbet_stats/nbbet_raw)."""
    p1_rank, p1_rank_tour = _rank(d.players.get("p1") if d.players else None)
    p2_rank, p2_rank_tour = _rank(d.players.get("p2") if d.players else None)
    detail_row = {
        "canonical_match_id": canonical_match_id,
        "nbbet_match_id": d.nbbet_match_id,
        "status": d.status,
        "has_coverage": d.has_coverage,
        "sets_json": json.dumps(d.sets, ensure_ascii=False),
        "extra_json": json.dumps(d.extra, ensure_ascii=False) if d.extra is not None else None,
        "players_json": json.dumps(d.players, ensure_ascii=False) if d.players is not None else None,
        "p1_rank": p1_rank,
        "p2_rank": p2_rank,
        "p1_rank_tour": p1_rank_tour,
        "p2_rank_tour": p2_rank_tour,
        "details_version": DETAILS_VERSION,
    }
    stat_rows = [{"canonical_match_id": canonical_match_id, **s} for s in d.stats]
    raw_row = {
        "canonical_match_id": canonical_match_id,
        "nbbet_match_id": d.nbbet_match_id,
        "raw_json": json.dumps(d.raw_page_tennis_event, ensure_ascii=False)
        if d.raw_page_tennis_event is not None else None,
    }
    return detail_row, stat_rows, raw_row
