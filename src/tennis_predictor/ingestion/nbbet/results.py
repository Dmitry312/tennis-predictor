"""Регулярный сбор результатов nb-bet (без point-by-point) в Match.
Замена collector_nb_bet.py — теперь пишет напрямую в Postgres.
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import insert, select, update

from tennis_predictor.ingestion.base import BaseCollector, CollectorResult
from tennis_predictor.ingestion.http import make_session
from tennis_predictor.ingestion.nbbet.pbp_parsing import (
    canonical_match_id,
    classify_tournament,
    is_doubles,
)
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.models import Match

logger = logging.getLogger(__name__)

BASE_URL = "https://app.nb-bet.com/v1/tennis/results/page"
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru",
    "Origin": "https://nb-bet.com",
    "Referer": "https://nb-bet.com/",
}


def _numeric_match_id(match_url: str) -> int | None:
    # Тот же способ, что в pbp_collector.py: ведущие цифры slug'а URL.
    m = re.match(r"^(\d+)", str(match_url))
    return int(m.group(1)) if m else None


class NbBetResultsCollector(BaseCollector):
    name = "nbbet-results"

    def __init__(self, *, days_back: int = 2):
        self.days_back = days_back
        self.session = make_session(headers=DEFAULT_HEADERS)

    def _fetch_day(self, day: date) -> list[dict]:
        ts = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
        resp = self.session.get(BASE_URL, params={"timestamp": ts}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        matches = []
        for league in data.get("data", {}).get("leagues", []):
            tournament_name = league.get("3", "")
            for m in league.get("4", []):
                match_url = m.get("3", "")
                match_time = m.get("4", 0)
                if not match_url or not match_time:
                    continue
                matches.append({
                    "match_url": match_url,
                    "tournament_name": tournament_name,
                    "match_timestamp": match_time,
                    "player1": m.get("7", ""),
                    "player2": m.get("15", ""),
                })
        return matches

    def run(self, *, dry_run: bool = False) -> CollectorResult:
        result = CollectorResult(source=self.name)
        today = datetime.now(tz=UTC).date()
        raw: list[dict] = []
        for offset in range(self.days_back):
            day = today - timedelta(days=offset)
            try:
                raw.extend(self._fetch_day(day))
            except Exception as e:  # noqa: BLE001 — один плохой день не должен рушить весь сбор
                result.errors.append(f"fetch_day({day}): {e}")

        candidates: dict[int, dict] = {}
        for m in raw:
            if is_doubles(m["player1"]) or is_doubles(m["player2"]):
                result.skipped += 1
                continue
            level_info = classify_tournament(m["tournament_name"])
            if level_info is None:
                result.skipped += 1
                continue
            nbbet_id = _numeric_match_id(m["match_url"])
            if nbbet_id is None:
                result.skipped += 1
                continue
            gender, level = level_info
            dt = datetime.fromtimestamp(m["match_timestamp"] / 1000, tz=UTC)
            candidates[nbbet_id] = {
                "match_id": canonical_match_id(nbbet_id),
                "gender": gender,
                "level": level,
                "tournament_name": m["tournament_name"],
                "match_date": dt.date(),
                "player1": m["player1"],
                "player2": m["player2"],
                "surface": None,
                "nbbet_match_id": nbbet_id,
                "championat_match_id": None,
                "has_pbp": False,
                "pbp_s3_path": None,
                "pbp_completeness": None,
            }

        if not candidates:
            result.log_summary()
            return result

        with engine.begin() as conn:
            existing = {
                row[0] for row in conn.execute(
                    select(Match.nbbet_match_id).where(
                        Match.nbbet_match_id.in_(candidates.keys())
                    )
                )
            }
            to_insert = [v for k, v in candidates.items() if k not in existing]
            # to_update — намеренно не трогаем has_pbp/pbp_s3_path здесь:
            # если матч уже есть с pbp (пришёл через pbp.py), нельзя эту
            # запись случайно затереть на has_pbp=False.
            to_update = [
                {"nbbet_match_id": k, "player1": v["player1"], "player2": v["player2"]}
                for k, v in candidates.items() if k in existing
            ]

            if to_insert and not dry_run:
                conn.execute(insert(Match), to_insert)
            if to_update and not dry_run:
                for row in to_update:
                    conn.execute(
                        update(Match)
                        .where(Match.nbbet_match_id == row["nbbet_match_id"])
                        .values(player1=row["player1"], player2=row["player2"])
                    )

        result.inserted = len(to_insert)
        result.updated = len(to_update)
        result.log_summary()
        return result
