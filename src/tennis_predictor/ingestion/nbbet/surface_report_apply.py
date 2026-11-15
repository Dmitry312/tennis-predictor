"""Apply a previously audited surface report; preview is the default.

Input: JSON report on stdin. No NB-Bet requests. Only tour/challenger,
only NULL surfaces, with full transactional revalidation.
"""
import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from sqlalchemy import text
from tennis_predictor.storage.db import engine

LEVELS = {"tour", "challenger"}
SURFACES = {"hard", "clay", "grass"}
VERIFY = text("""
SELECT COUNT(*) FILTER (WHERE surface IS NULL) AS missing,
 COUNT(DISTINCT surface) FILTER (WHERE surface IS NOT NULL) AS known_count,
 MIN(surface) FILTER (WHERE surface IS NOT NULL) AS known,
 COUNT(*) FILTER (WHERE surface IS NOT NULL AND surface NOT IN ('hard','clay','grass')) AS invalid
FROM public.matches WHERE duplicate_of IS NULL
 AND level IN ('tour','challenger') AND level = :level
 AND tournament_name = :name AND gender = :gender
 AND match_date >= make_date(:year,1,1) AND match_date < make_date(:year+1,1,1)
""")
UPDATE = text("""
UPDATE public.matches SET surface = :surface
WHERE duplicate_of IS NULL AND surface IS NULL
 AND level IN ('tour','challenger') AND level = :level
 AND tournament_name = :name AND gender = :gender
 AND match_date >= make_date(:year,1,1) AND match_date < make_date(:year+1,1,1)
""")

def load_report(data):
    if not isinstance(data, dict) or data.get("apply") is not False or data.get("matches_updated") != 0:
        raise ValueError("Only a dry-run report with zero updates is accepted")
    year = data.get("year")
    if type(year) is not int or not 2010 <= year <= datetime.now(UTC).year:
        raise ValueError("Invalid report year")
    groups = data.get("groups")
    if not isinstance(groups, list) or len(groups) != data.get("groups_examined"):
        raise ValueError("Incomplete report")
    keys = [(g.get("name"), g.get("gender"), g.get("level"), g.get("year")) for g in groups]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate report groups")
    selected = []
    for g in groups:
        if g.get("status") != "dry_run_eligible":
            continue
        if (g.get("level") not in LEVELS or g.get("gender") not in ("M","F")
            or not isinstance(g.get("name"), str) or not g["name"]
            or g.get("year") != year or g.get("proposed_surface") not in SURFACES
            or type(g.get("missing_before")) is not int or g["missing_before"] <= 0
            or "davis cup" in g["name"].casefold() or "кубок дэвиса" in g["name"].casefold()):
            raise ValueError("Unsafe eligible group")
        selected.append(g)
    if sum(g["missing_before"] for g in selected) != data.get("potential_matches"):
        raise ValueError("Report totals mismatch")
    return year, selected

def run(data, apply):
    year, groups = load_report(data)
    summary = {"year": year, "apply": apply, "groups_in_report": len(data["groups"]),
               "eligible_groups": len(groups), "report_potential_matches": data["potential_matches"],
               "matches_updated": 0, "status_counts": {}, "groups": []}
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text("SET LOCAL statement_timeout = '30s'"))
        if apply:
            # Serialize report applications; fail instead of waiting indefinitely.
            locked = conn.execute(text("SELECT pg_try_advisory_xact_lock(729231, :year)"), {"year": year}).scalar()
            if not locked:
                raise RuntimeError("Another report application is running")
        else:
            conn.execute(text("SET TRANSACTION READ ONLY"))
        # First verify ALL groups. Any stale/conflicting group aborts apply entirely.
        checked = []
        for g in groups:
            params = {"year": year, "name": g["name"], "gender": g["gender"],
                      "level": g["level"], "surface": g["proposed_surface"]}
            current = conn.execute(VERIFY, params).mappings().one()
            ready = (current["missing"] == g["missing_before"]
                     and current["invalid"] == 0 and current["known_count"] <= 1
                     and (current["known_count"] == 0 or current["known"] == g["proposed_surface"]))
            checked.append((g, params, ready))
        if apply and not all(x[2] for x in checked):
            raise RuntimeError("Report is stale or conflicting; zero writes committed")
        for g, params, ready in checked:
            n = 0
            if apply:
                # Revalidate directly before each write as well.
                current = conn.execute(VERIFY, params).mappings().one()
                if (current["missing"] != g["missing_before"] or current["invalid"]
                    or current["known_count"] > 1 or
                    (current["known_count"] == 1 and current["known"] != g["proposed_surface"])):
                    raise RuntimeError("Changed during apply; transaction rolled back")
                n = conn.execute(UPDATE, params).rowcount
                if n != g["missing_before"]:
                    raise RuntimeError("Unexpected update count; transaction rolled back")
            summary["groups"].append({"name": g["name"], "gender": g["gender"],
                                      "level": g["level"], "surface": g["proposed_surface"],
                                      "missing_before": g["missing_before"],
                                      "status": ("applied" if apply else "ready") if ready else "changed_or_conflicting",
                                      "updated": n})
            summary["matches_updated"] += n
    summary["status_counts"] = dict(Counter(g["status"] for g in summary["groups"]))
    summary["utc"] = datetime.now(UTC).isoformat()
    return summary

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    import sys
    data = json.load(sys.stdin)
    print(json.dumps(run(data, args.apply), ensure_ascii=False))

if __name__ == "__main__":
    main()
