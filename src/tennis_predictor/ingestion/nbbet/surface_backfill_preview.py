"""Read-only tournament surface backfill preview. No production UPDATEs.

Group by exact tournament_name, gender and level across all years.
Known surfaces are evidence; ambiguous groups are never approved.
Optional live probes request at most two distinct match pages per group.
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
from tennis_predictor.storage.db import engine

SURFACES = frozenset({"hard", "clay", "grass"})

# Read-only, repeatable-read snapshot. No S3 or PBP access.
GROUPS_SQL = text("""
WITH grouped AS (
 SELECT tournament_name, gender, level,
        COUNT(*) AS matches_total,
        COUNT(*) FILTER (WHERE surface IS NULL) AS missing,
        COUNT(DISTINCT surface) FILTER (WHERE surface IS NOT NULL) AS distinct_known,
        MIN(surface) FILTER (WHERE surface IS NOT NULL) AS known_surface,
        COUNT(*) FILTER (WHERE surface IS NOT NULL AND surface NOT IN ('hard','clay','grass')) AS invalid_known
 FROM public.matches
 WHERE duplicate_of IS NULL AND tournament_name IS NOT NULL
 GROUP BY tournament_name, gender, level
)
SELECT tournament_name, gender, level, matches_total, missing,
       distinct_known, known_surface, invalid_known
FROM grouped
WHERE missing > 0
ORDER BY missing DESC, tournament_name COLLATE "C", gender, level
""")

CANDIDATES_SQL = text("""
SELECT nbbet_match_id, nbbet_slug, match_date
FROM public.matches
WHERE duplicate_of IS NULL AND tournament_name IS NOT DISTINCT FROM :name
  AND gender IS NOT DISTINCT FROM :gender
  AND level IS NOT DISTINCT FROM :level
  AND nbbet_slug IS NOT NULL AND nbbet_match_id IS NOT NULL
ORDER BY match_date DESC NULLS LAST, nbbet_match_id DESC
LIMIT 2
""")


def decide(known: list[str], probes: list[str | None], required: int) -> tuple[str, str | None]:
    """Require two independent observations unless existing DB evidence is unanimous."""
    if any(value not in SURFACES for value in known):
        return "invalid_existing", None
    if len(set(known)) > 1:
        return "conflict_existing", None
    observed = [v for v in probes if v in SURFACES]
    if any(v is not None and v not in SURFACES for v in probes):
        return "invalid_probe", None
    if len(set(observed + known)) > 1:
        return "conflict_probe", None
    if known:
        return "known_consistent", known[0]
    if len(observed) < required:
        return "insufficient_evidence", None
    return "probed_consistent", observed[0]


def preview(max_groups: int, probe: bool, pause: float) -> dict:
    if max_groups < 1 or max_groups > 100:
        raise ValueError("max_groups must be between 1 and 100")
    if pause < 1:
        raise ValueError("pause must be >= 1 second")
    session = make_session(headers=HEADERS, retry_statuses=(500, 502, 503, 504)) if probe else None
    results = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        groups = conn.execute(GROUPS_SQL).mappings().all()
        for g in groups[:max_groups]:
            known = [g["known_surface"]] if g["distinct_known"] == 1 else (
                ["hard", "clay"] if g["distinct_known"] > 1 else []
            )
            if g["invalid_known"]:
                status, surface = "invalid_existing", None
            elif g["distinct_known"] > 1:
                status, surface = "conflict_existing", None
            elif known:
                status, surface = decide(known, [], 2)
            elif not probe:
                status, surface = "needs_probe", None
            else:
                candidates = conn.execute(CANDIDATES_SQL, {
                    "name": g["tournament_name"], "gender": g["gender"], "level": g["level"]
                }).mappings().all()
                found = []
                for c in candidates:
                    try:
                        html = fetch_match_page(session, c["nbbet_slug"])
                        found.append(parse_match_page(html, c["nbbet_match_id"]).surface if html else None)
                    except Exception:
                        found.append(None)
                    time.sleep(pause)
                status, surface = decide([], found, 2)
            results.append({
                "tournament_name": g["tournament_name"],
                "gender": g["gender"], "level": g["level"],
                "matches_total": g["matches_total"], "missing": g["missing"],
                "status": status, "proposed_surface": surface,
            })
        conn.rollback()
    return {
        "audit_utc": datetime.now(UTC).isoformat(),
        "mode": "live_probe_read_only" if probe else "db_only_read_only",
        "groups_total_with_missing": len(groups),
        "groups_examined": len(results),
        "matches_missing_examined": sum(r["missing"] for r in results),
        "matches_potentially_fillable": sum(
            r["missing"] for r in results if r["proposed_surface"] is not None
        ),
        "status_counts": dict(Counter(r["status"] for r in results)),
        "groups": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-groups", type=int, default=20)
    parser.add_argument("--probe", action="store_true",
                        help="opt in to up to two NB-Bet HTTP requests per unresolved group")
    parser.add_argument("--pause", type=float, default=2.0)
    args = parser.parse_args()
    print(json.dumps(preview(args.max_groups, args.probe, args.pause), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
