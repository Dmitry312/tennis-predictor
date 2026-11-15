"""Read-only revalidation of an existing surface dry-run report.

REPORT_BYTES must contain the downloaded JSON report (injected by workflow).
No HTTP calls to NB-Bet and no database writes.
"""
import json
from collections import Counter
from datetime import UTC, datetime
from sqlalchemy import text
from tennis_predictor.storage.db import engine

ALLOWED = {"tour", "challenger"}
SURFACES = {"hard", "clay", "grass"}
SQL = text("""
SELECT COUNT(*) FILTER (WHERE surface IS NULL) AS missing,
 COUNT(DISTINCT surface) FILTER (WHERE surface IS NOT NULL) AS known_count,
 MIN(surface) FILTER (WHERE surface IS NOT NULL) AS known,
 COUNT(*) FILTER (WHERE surface IS NOT NULL AND surface NOT IN ('hard','clay','grass')) AS invalid
FROM public.matches
WHERE duplicate_of IS NULL
 AND tournament_name IS NOT DISTINCT FROM :name
 AND gender IS NOT DISTINCT FROM :gender
 AND level IS NOT DISTINCT FROM :level
 AND match_date >= make_date(:year,1,1)
 AND match_date < make_date(:year+1,1,1)
""")

def main():
    report = json.loads(REPORT_BYTES)
    if report.get("year") != 2025 or report.get("apply") is not False:
        raise ValueError("Expected 2025 dry-run report")
    if report.get("matches_updated") != 0:
        raise ValueError("Report must not contain writes")
    groups = report.get("groups")
    if not isinstance(groups, list) or len(groups) != report.get("groups_examined"):
        raise ValueError("Incomplete report")
    if len(groups) != len({(g.get("name"), g.get("gender"), g.get("level"), g.get("year")) for g in groups}):
        raise ValueError("Duplicate report groups")
    eligible = [g for g in groups if g.get("status") == "dry_run_eligible"]
    if not eligible or any(g.get("level") not in ALLOWED or g.get("year") != 2025
                           or g.get("proposed_surface") not in SURFACES for g in eligible):
        raise ValueError("Invalid eligible group")
    if sum(g["missing_before"] for g in eligible) != report.get("potential_matches"):
        raise ValueError("Report totals do not reconcile")
    results = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout = '15s'"))
        for g in eligible:
            row = conn.execute(SQL, {k: g[k] for k in ("name", "gender", "level", "year")}).mappings().one()
            surface = g["proposed_surface"]
            ok = (row["missing"] == g["missing_before"] and row["missing"] > 0
                  and row["invalid"] == 0 and row["known_count"] <= 1
                  and (row["known_count"] == 0 or row["known"] == surface))
            results.append({"name": g["name"], "gender": g["gender"], "level": g["level"],
                            "surface": surface, "report_missing": g["missing_before"],
                            "current_missing": row["missing"],
                            "status": "ready" if ok else "changed_or_conflicting"})
        conn.rollback()
    counts = Counter(x["status"] for x in results)
    print(json.dumps({
        "utc": datetime.now(UTC).isoformat(), "year": 2025, "apply": False,
        "allowed_levels": sorted(ALLOWED), "report_groups": len(groups),
        "eligible_groups": len(eligible), "status_counts": dict(counts),
        "ready_matches": sum(x["current_missing"] for x in results if x["status"] == "ready"),
        "blocked_matches_in_report": sum(x["report_missing"] for x in results if x["status"] != "ready"),
        "groups": results,
    }, ensure_ascii=False, default=str))

if __name__ == "__main__":
    main()
