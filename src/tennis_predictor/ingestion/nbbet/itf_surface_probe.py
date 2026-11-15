"""Read-only verification of two ITF NB-Bet match pages and DB mappings.

Run: python -m tennis_predictor.ingestion.nbbet.itf_surface_probe
Does not update PostgreSQL, S3, or any match records.
"""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime

from sqlalchemy import text

from tennis_predictor.ingestion.http import make_session
from tennis_predictor.ingestion.nbbet.details import fetch_match_page, parse_match_page
from tennis_predictor.ingestion.nbbet.discovery import HEADERS, _fetch_day
from tennis_predictor.storage.db import engine

SAMPLES = (
    ("766594-valeriya-yushchenko-diana-ioana-simionescu-prognoz-na-match", "clay"),
    ("766593-vissam-abderrahman-zhyulen-penzlin-prognoz-na-match", "hard"),
)
SQL = text("""
SELECT match_id::text AS match_id, nbbet_match_id, nbbet_slug,
       tournament_name, gender, level, match_date, player1, player2,
       surface, has_pbp, details_fetched_at
FROM public.matches WHERE nbbet_match_id = :id
ORDER BY match_date DESC NULLS LAST LIMIT 5
""")
GROUP_SQL = text("""
SELECT COUNT(*) AS matches, COUNT(*) FILTER (WHERE surface IS NOT NULL) AS with_surface,
       COUNT(*) FILTER (WHERE nbbet_slug IS NOT NULL) AS with_slug
FROM public.matches WHERE level = 'itf' AND tournament_name IS NOT DISTINCT FROM :name
  AND gender IS NOT DISTINCT FROM :gender
  AND match_date >= :date - INTERVAL '21 days'
  AND match_date <= :date + INTERVAL '21 days'
""")

def run() -> dict:
    samples = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout = '15s'"))
        for slug, expected in SAMPLES:
            mid = int(slug.split("-", 1)[0])
            matches = [dict(r) for r in conn.execute(SQL, {"id": mid}).mappings()]
            group = None
            if matches and matches[0]["match_date"] is not None:
                row = matches[0]
                group = dict(conn.execute(GROUP_SQL, {
                    "name": row["tournament_name"], "gender": row["gender"],
                    "date": row["match_date"],
                }).mappings().one())
            samples.append({"id": mid, "slug": slug, "expected_surface": expected,
                            "db_matches": matches, "nearby_tournament_group": group})
        conn.rollback()

    session = make_session(headers=HEADERS, retry_statuses=())
    for idx, sample in enumerate(samples):
        try:
            html = fetch_match_page(session, sample["slug"])
            if html is None:
                sample["page"] = {"status": "not_found"}
            else:
                parsed = parse_match_page(html, sample["id"])
                sample["page"] = {
                    "status": "parsed",
                    "surface": parsed.surface,
                    "matches_expected_surface": parsed.surface == sample["expected_surface"],
                    "stats_scopes": len(parsed.stats),
                    "has_coverage": parsed.has_coverage,
                }
        except Exception as exc:
            sample["page"] = {"status": "error", "error_type": type(exc).__name__,
                              "http_status": getattr(getattr(exc, "response", None), "status_code", None)}
        # Verify discovery using the stored match date (at most one request per distinct date).
        if idx + 1 < len(samples):
            time.sleep(3)

    dates = sorted({row["match_date"] for s in samples for row in s["db_matches"]
                    if row["match_date"] is not None})
    listing_results = []
    for day in dates[:2]:
        time.sleep(3)
        try:
            raw = _fetch_day(session, day)
            hits = [r for r in raw if any(re.match(r"^" + str(s["id"]) + r"(?:-|$)", str(r["slug"]))
                                             for s in samples)]
            listing_results.append({"date": str(day), "matches_on_day": len(raw),
                                    "sample_hits": [{"id": int(r["slug"].split("-", 1)[0]),
                                                     "slug": r["slug"],
                                                     "tournament_name": r["tournament_name"]}
                                                    for r in hits]})
        except Exception as exc:
            listing_results.append({"date": str(day), "error_type": type(exc).__name__,
                                    "http_status": getattr(getattr(exc, "response", None), "status_code", None)})

    return {"utc": datetime.now(UTC).isoformat(), "read_only": True,
            "db_updated": 0, "samples": samples, "discovery_listing": listing_results,
            "notes": ["Only two explicitly supplied pages are fetched",
                      "Listing checks use stored match dates; matches may appear on adjacent listing dates",
                      "No slug recovery or bulk backfill is performed"]}

if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, default=str))
