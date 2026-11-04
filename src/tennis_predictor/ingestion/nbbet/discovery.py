"""Общий discovery nb-bet: кандидаты за диапазон дней со статусом.

Статус в листинге лежит в поле "1" (enum см. status.py), на странице
матча то же значение в поле "23".
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from tennis_predictor.ingestion.http import make_session
from tennis_predictor.ingestion.nbbet.odds import odds_pair
from tennis_predictor.ingestion.nbbet.pbp_parsing import classify_tournament, is_doubles

logger = logging.getLogger(__name__)

BASE_URL = "https://app.nb-bet.com/v1/tennis/results/page"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru",  # без него названия турниров приходят по-английски
    "Origin": "https://nb-bet.com",
    "Referer": "https://nb-bet.com/",
}


@dataclass(frozen=True)
class NbBetCandidate:
    nbbet_match_id: int
    slug: str
    tournament_name: str
    gender: str
    level: str
    match_date: date
    player1: str
    player2: str
    status: int | None
    start_ts: datetime
    odds_raw: dict | None = None  # {"5": .., "6": ..} листинга как есть; None = обоих нет
    odds_open: tuple[float, float] | None = None  # odds_pair(odds_raw["6"]) — начальные цены


def _numeric_match_id(slug: str) -> int | None:
    m = re.match(r"^(\d+)", str(slug))
    return int(m.group(1)) if m else None


def _fetch_day(session, day: date) -> list[dict]:
    ts = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    resp = session.get(BASE_URL, params={"timestamp": ts}, timeout=20)
    resp.raise_for_status()
    out = []
    for league in resp.json().get("data", {}).get("leagues", []):
        tournament_name = league.get("3", "")
        for m in league.get("4", []):
            out.append({
                "slug": m.get("3", ""),
                "tournament_name": tournament_name,
                "ts": m.get("4", 0),
                "player1": m.get("7", ""),
                "player2": m.get("15", ""),
                "status": m.get("1"),
                "odds_5": m.get("5"),
                "odds_6": m.get("6"),
            })
    return out


def discover_range(
    start: date, end: date, *, session=None, strict: bool = False
) -> list[NbBetCandidate]:
    """Кандидаты за [start, end] включительно, без парных и вне LEVEL_MAP.
    Если матч встретился в нескольких днях, остаётся более поздняя запись.

    strict=True пробрасывает ошибку дня вместо логирования и пропуска —
    нужно sweep.py, чтобы отличить упавший день от действительно пустого
    и не закрыть его молча."""
    session = session or make_session(headers=HEADERS)
    by_id: dict[int, NbBetCandidate] = {}
    day = start
    while day <= end:
        try:
            raw = _fetch_day(session, day)
        except Exception as e:
            if strict:
                raise
            logger.error("discover: день %s не загрузился: %s", day, e)
            day += timedelta(days=1)
            continue
        for m in raw:
            if is_doubles(m["player1"]) or is_doubles(m["player2"]):
                continue
            info = classify_tournament(m["tournament_name"])
            nbbet_id = _numeric_match_id(m["slug"])
            if info is None or nbbet_id is None or not m["ts"]:
                continue
            gender, level = info
            odds_5, odds_6 = m.get("odds_5"), m.get("odds_6")
            odds_raw = {"5": odds_5, "6": odds_6} if (odds_5 is not None or odds_6 is not None) else None
            odds_open = odds_pair(odds_6)
            by_id[nbbet_id] = NbBetCandidate(
                nbbet_match_id=nbbet_id,
                slug=m["slug"],
                tournament_name=m["tournament_name"],
                gender=gender,
                level=level,
                match_date=datetime.fromtimestamp(m["ts"] / 1000, tz=UTC).date(),
                player1=m["player1"],
                player2=m["player2"],
                status=m["status"],
                start_ts=datetime.fromtimestamp(m["ts"] / 1000, tz=UTC).replace(tzinfo=None),
                odds_raw=odds_raw,
                odds_open=odds_open,
            )
        day += timedelta(days=1)
    logger.info("discover %s..%s: %d кандидатов", start, end, len(by_id))
    return list(by_id.values())


def discover(days_back: int = 2, *, session=None) -> list[NbBetCandidate]:
    today = datetime.now(tz=UTC).date()
    return discover_range(today - timedelta(days=days_back - 1), today, session=session)
