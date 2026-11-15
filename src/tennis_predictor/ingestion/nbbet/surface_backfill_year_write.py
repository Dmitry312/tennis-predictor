"""Conservative year-by-year surface backfill; dry-run unless --apply.

For each tournament/gender/level/year, use two distinct NB-Bet match pages.
Never overwrite existing surfaces. Skip Davis Cup and contradictory groups.
Every eligible group in the selected year is visited in one run.
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

SURFACES = frozenset(("hard", "clay", "grass"))
GROUPS_SQL = text("""
SELECT tournament_name, gender, level,
 COUNT(*) FILTER (WHERE surface IS NULL) AS missing,
 COUNT(DISTINCT surface) FILTER (WHERE surface IS NOT NULL) AS known_count,
 MIN(surface) FILTER (WHERE surface IS NOT NULL) AS known,
 COUNT(*) FILTER (WHERE surface IS NOT NULL AND surface NOT IN ('hard','clay','grass')) AS invalid
FROM public.matches
WHERE duplicate_of IS NULL AND tournament_name IS NOT NULL
 AND match_date >= make_date(:year,1,1)
 AND match_date < make_date(:year + 1,1,1)
GROUP BY tournament_name, gender, level
HAVING COUNT(*) FILTER (WHERE surface IS NULL) > 0
ORDER BY missing DESC, tournament_name COLLATE "C", gender, level
""")
CANDIDATES_SQL = text("""
SELECT nbbet_match_id, nbbet_slug FROM public.matches
WHERE duplicate_of IS NULL
 AND tournament_name IS NOT DISTINCT FROM :name
 AND gender IS NOT DISTINCT FROM :gender AND level IS NOT DISTINCT FROM :level
 AND match_date >= make_date(:year,1,1)
 AND match_date < make_date(:year+1,1,1)
 AND nbbet_slug IS NOT NULL AND nbbet_match_id IS NOT NULL
ORDER BY match_date DESC NULLS LAST, nbbet_match_id DESC LIMIT 2
""")
UPDATE_SQL = text("""
UPDATE public.matches SET surface = :surface
WHERE duplicate_of IS NULL AND surface IS NULL
 AND tournament_name IS NOT DISTINCT FROM :name
 AND gender IS NOT DISTINCT FROM :gender AND level IS NOT DISTINCT FROM :level
 AND match_date >= make_date(:year,1,1)
 AND match_date < make_date(:year+1,1,1)
""")
VERIFY_SQL = text("""
SELECT COUNT(*) FILTER (WHERE surface IS NULL) AS missing,
 COUNT(DISTINCT surface) FILTER (WHERE surface IS NOT NULL) AS known_count,
 MIN(surface) FILTER (WHERE surface IS NOT NULL) AS known,
 COUNT(*) FILTER (WHERE surface IS NOT NULL AND surface NOT IN ('hard','clay','grass')) AS invalid
FROM public.matches WHERE duplicate_of IS NULL
 AND tournament_name IS NOT DISTINCT FROM :name
 AND gender IS NOT DISTINCT FROM :gender AND level IS NOT DISTINCT FROM :level
 AND match_date >= make_date(:year,1,1)
 AND match_date < make_date(:year+1,1,1)
""")


def blocked(name: str) -> bool:
    name = name.casefold()
    return "davis cup" in name or "кубок дэвиса" in name


def eligible(known_count: int, known: str | None, invalid: int, probes: list[str | None]) -> str | None:
    if invalid or known_count > 1 or len(probes) != 2 or any(p not in SURFACES for p in probes):
        return None
    if probes[0] != probes[1]:
        return None
    if known_count == 1 and known != probes[0]:
        return None
    return probes[0]


def run(year: int, apply: bool, pause: float) -> dict:
    if not 2010 <= year <= datetime.now(UTC).year or pause < 2:
        raise ValueError("year must be 2010..current and pause >= 2")
    session = make_session(headers=HEADERS, retry_statuses=())
    report = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        groups = conn.execute(GROUPS_SQL, {"year": year}).mappings().all()
        tasks = []
        for group in groups:
            params = dict(year=year, name=group["tournament_name"],
                          gender=group["gender"], level=group["level"])
            candidates = (conn.execute(CANDIDATES_SQL, params).mappings().all()
                          if not blocked(group["tournament_name"]) and not group["invalid"]
                          and group["known_count"] <= 1 else [])
            tasks.append((dict(group), params, candidates))
        conn.rollback()

    for group, params, candidates in tasks:
        row = dict(name=params["name"], gender=params["gender"], level=params["level"],
                   year=year, missing_before=group["missing"], updated=0)
        if blocked(params["name"]):
            row["status"] = "excluded_davis_cup"
        elif group["invalid"] or group["known_count"] > 1:
            row["status"] = "conflicting_existing"
        elif len(candidates) != 2 or candidates[0]["nbbet_match_id"] == candidates[1]["nbbet_match_id"]:
            row["status"] = "insufficient_pages"
        else:
            probes = []
            for candidate in candidates:
                try:
                    html = fetch_match_page(session, candidate["nbbet_slug"])
                    probes.append(parse_match_page(html, candidate["nbbet_match_id"]).surface if html else None)
                except Exception as exc:
                    if getattr(getattr(exc, "response", None), "status_code", None) in (403, 429):
                        raise RuntimeError("NB-Bet blocked or rate-limited the job") from None
                    probes.append(None)
                time.sleep(pause)
            surface = eligible(group["known_count"], group["known"], group["invalid"], probes)
            if surface is None:
                row["status"] = "insufficient_or_conflicting_probes"
            else:
                row["proposed_surface"] = surface
                row["status"] = "dry_run_eligible" if not apply else "pending_write"
                if apply:
                    # SERIALIZABLE is intentionally avoided; row locks + revalidation
                    # prevent overwriting non-NULL values even with active collectors.
                    with engine.begin() as write_conn:
                        write_conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                        write_conn.execute(text("SET LOCAL statement_timeout = '30s'"))
                        current = write_conn.execute(VERIFY_SQL, params).mappings().one()
                        if (current["missing"] != group["missing"] or current["invalid"] or current["known_count"] > 1
                                or (current["known_count"] == 1 and current["known"] != surface)):
                            row["status"] = "skipped_changed_evidence"
                        else:
                            result = write_conn.execute(UPDATE_SQL, {**params, "surface": surface})
                            row["updated"] = result.rowcount
                            row["status"] = "applied"
        report.append(row)
        if len(report) % 25 == 0:
            import sys
            print(f"Processed {len(report)}/{len(tasks)} groups for {year}", file=sys.stderr, flush=True)
    return {
        "utc": datetime.now(UTC).isoformat(), "year": year, "apply": apply,
        "groups_examined": len(report), "groups_total_in_year": len(tasks),
        "potential_matches": sum(r["missing_before"] for r in report if r.get("proposed_surface")),
        "matches_updated": sum(r["updated"] for r in report),
        "status_counts": dict(Counter(r["status"] for r in report)),
        "groups": report,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--pause", type=float, default=3)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.year, args.apply, args.pause),
                     ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
