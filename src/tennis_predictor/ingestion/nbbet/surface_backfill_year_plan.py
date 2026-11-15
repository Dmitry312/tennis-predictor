"""Read-only, year-scoped surface backfill plan. Never writes to PostgreSQL.

Tournament names alone are not a safe key: surfaces may change across seasons.
Davis Cup is likewise scoped by year; a venue/tie-specific source is required
before assuming a common surface across all its matches.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import text

from tennis_predictor.ingestion.http import make_session
from tennis_predictor.ingestion.nbbet.details import fetch_match_page, parse_match_page
from tennis_predictor.ingestion.nbbet.discovery import HEADERS
from tennis_predictor.ingestion.nbbet.surface_backfill_preview import decide
from tennis_predictor.storage.db import engine

GROUPS_SQL = text("""
SELECT tournament_name, gender, level, EXTRACT(YEAR FROM match_date)::integer AS season,
       COUNT(*) AS total,
       COUNT(*) FILTER (WHERE surface IS NULL) AS missing,
       COUNT(DISTINCT surface) FILTER (WHERE surface IS NOT NULL) AS known_count,
       MIN(surface) FILTER (WHERE surface IS NOT NULL) AS known,
       COUNT(*) FILTER (WHERE surface IS NOT NULL
                        AND surface NOT IN ('hard','clay','grass')) AS invalid
FROM public.matches
WHERE duplicate_of IS NULL AND tournament_name IS NOT NULL
GROUP BY tournament_name, gender, level, EXTRACT(YEAR FROM match_date)::integer
HAVING COUNT(*) FILTER (WHERE surface IS NULL) > 0
ORDER BY missing DESC, tournament_name COLLATE "C", gender, level, season
""")

CANDIDATES_SQL = text("""
SELECT nbbet_match_id, nbbet_slug
FROM public.matches
WHERE duplicate_of IS NULL AND tournament_name IS NOT DISTINCT FROM :name
 AND gender IS NOT DISTINCT FROM :gender AND level IS NOT DISTINCT FROM :level
 AND EXTRACT(YEAR FROM match_date)::integer IS NOT DISTINCT FROM :season
 AND nbbet_match_id IS NOT NULL AND nbbet_slug IS NOT NULL
ORDER BY match_date DESC NULLS LAST, nbbet_match_id DESC
LIMIT 2
""")


def requires_tie_level(name: str) -> bool:
    """Davis Cup can use different surfaces between ties within one season."""
    lowered = name.casefold()
    return "davis cup" in lowered or "кубок дэвиса" in lowered


def plan(limit: int, probe: bool, pause: float) -> dict:
    if not 1 <= limit <= 20 or pause < 1:
        raise ValueError("limit must be 1..20 and pause >= 1")
    results, pending = [], []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        groups = conn.execute(GROUPS_SQL).mappings().all()
        for g in groups[:limit]:
            row = dict(name=g["tournament_name"], gender=g["gender"], level=g["level"],
                       season=g["season"], matches_total=g["total"], missing=g["missing"])
            if g["season"] is None:
                status, surface = "missing_date", None
            elif requires_tie_level(g["tournament_name"]):
                status, surface = "tie_level_required", None
            elif g["invalid"]:
                status, surface = "invalid_existing", None
            elif g["known_count"] > 1:
                status, surface = "conflict_existing", None
            elif g["known_count"] == 1:
                status, surface = decide([g["known"]], [], 2)
            elif not probe:
                status, surface = "needs_probe", None
            else:
                candidates = conn.execute(CANDIDATES_SQL, {
                    "name": g["tournament_name"], "gender": g["gender"],
                    "level": g["level"], "season": g["season"]
                }).mappings().all()
                pending.append((len(results), list(candidates)))
                status, surface = "needs_probe", None
            row.update(status=status, proposed_surface=surface)
            results.append(row)
        conn.rollback()

    session = make_session(headers=HEADERS, retry_statuses=()) if pending else None
    for index, candidates in pending:
        observations = []
        for candidate in candidates:
            try:
                html = fetch_match_page(session, candidate["nbbet_slug"])
                observations.append(
                    parse_match_page(html, candidate["nbbet_match_id"]).surface if html else None
                )
            except Exception as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) in (403, 429):
                    raise RuntimeError("NB-Bet access blocked or rate limited") from None
                observations.append(None)
            time.sleep(pause)
        status, surface = decide([], observations, 2)
        results[index].update(status=status, proposed_surface=surface)

    return {
        "audit_utc": datetime.now(UTC).isoformat(),
        "mode": "year_scoped_probe_read_only" if probe else "year_scoped_db_read_only",
        "groups_total_with_missing": len(groups),
        "groups_examined": len(results),
        "matches_missing_examined": sum(row["missing"] for row in results),
        "matches_potentially_fillable": sum(
            row["missing"] for row in results if row["proposed_surface"] is not None
        ),
        "status_counts": dict(Counter(row["status"] for row in results)),
        "groups": results,
        "write_performed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-groups", type=int, default=20)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--pause", type=float, default=3.0)
    args = parser.parse_args()
    print(json.dumps(plan(args.max_groups, args.probe, args.pause), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
