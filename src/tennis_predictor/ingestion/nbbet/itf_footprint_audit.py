"""Read-only ITF coverage and detail-attempt footprint; never deletes data."""
import json
from datetime import UTC, datetime
from sqlalchemy import text
from tennis_predictor.storage.db import engine

SQL = text("""
SELECT level, COUNT(*) AS matches,
 COUNT(*) FILTER (WHERE duplicate_of IS NULL) AS canonical_matches,
 COUNT(*) FILTER (WHERE has_pbp IS TRUE) AS has_pbp,
 COUNT(*) FILTER (WHERE details_fetched_at IS NOT NULL) AS details_fetched,
 COUNT(*) FILTER (WHERE surface IS NOT NULL) AS has_surface,
 COUNT(*) FILTER (WHERE odds_open_p1 IS NOT NULL AND odds_open_p2 IS NOT NULL) AS has_odds,
 COUNT(*) FILTER (WHERE nbbet_slug IS NOT NULL) AS has_slug,
 COUNT(*) FILTER (WHERE nbbet_match_id IS NOT NULL) AS has_nbbet_id,
 COUNT(*) FILTER (WHERE nbbet_slug IS NOT NULL AND nbbet_match_id IS NOT NULL) AS has_both_ids,
 COUNT(*) FILTER (WHERE details_attempts > 0) AS matches_with_attempts,
 COALESCE(SUM(details_attempts), 0) AS stored_detail_attempts,
 COUNT(*) FILTER (WHERE details_fetched_at IS NULL AND details_attempts >= 5) AS exhausted_detail_attempts
FROM public.matches GROUP BY ROLLUP(level)
ORDER BY level NULLS LAST
""")

def main():
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout = '30s'"))
        rows = [dict(row) for row in conn.execute(SQL).mappings()]
        conn.rollback()
    # ROLLUP creates a total with level NULL, even if actual NULL levels exist.
    # GROUPING(level) is required to distinguish those cases.
    total = rows[-1]
    itf = next((r for r in rows if r["level"] == "itf"), None)
    print(json.dumps({
        "audit_utc": datetime.now(UTC).isoformat(),
        "read_only": True, "deleted": 0,
        "itf_share_of_matches": round(itf["matches"] / total["matches"], 4) if itf and total["matches"] else None,
        "itf_share_of_stored_detail_attempts": round(itf["stored_detail_attempts"] / total["stored_detail_attempts"], 4) if itf and total["stored_detail_attempts"] else None,
        "limitations": ["details_attempts is not HTTP request count", "has_pbp is a DB flag, not validated PBP completeness", "details_fetched does not prove nonempty statistics", "HTTP traffic and S3 byte usage are not measured"],
        "levels": rows,
    }, ensure_ascii=False, default=str))

if __name__ == "__main__":
    main()
