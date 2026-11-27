"""Read-only targeted audit; run on production with PYTHONPATH=src.
Scans raw/stats partitions once per group, retains only target matches.
"""
import json
import time
from collections import defaultdict
from sqlalchemy import select
from tennis_predictor.ingestion.nbbet import stats_crosscheck as sc
from tennis_predictor.ingestion.nbbet.details import parse_match_page
from tennis_predictor.storage.models import Match

IDS = (249197, 26041, 577961, 81415, 335934, 725766, 94721,
       453599, 135037, 691300, 605600, 571806)


def inspect_raw(event, mid):
    match = event.get("match") or {}
    html = '<script id="__NEXT_DATA__">' + json.dumps(
        {"props": {"initialState": {"pageTennisEvent": event}}}) + '</script>'
    parsed = parse_match_page(html, mid)
    return {"status": parsed.status, "coverage": parsed.has_coverage,
            "source_stats": match.get("25"), "parsed_stats": parsed.stats}


def main():
    with sc.engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql("SET LOCAL statement_timeout = '10s'")
            matches = conn.execute(select(Match.match_id, Match.nbbet_match_id,
                Match.nbbet_slug, Match.gender, Match.level, Match.match_date,
                Match.pbp_s3_path, Match.details_fetched_at, Match.details_version,
                Match.status, Match.duplicate_of).where(
                    Match.nbbet_match_id.in_(IDS))).all()
    groups = defaultdict(list)
    result = {}
    for m in matches:
        groups[(m.gender, m.level, m.match_date.year)].append(m)
        result[str(m.match_id)] = {"db": dict(m._mapping), "raw": [], "stats": [],
                                   "scan_errors": [], "live": None}
    s3, bucket = sc.get_s3_client(), sc.get_settings().s3_bucket
    session = sc.make_session(headers=sc.HEADERS, retry_statuses=(500, 502, 503, 504))
    for m in matches:
        report = result[str(m.match_id)]
        try:
            html = sc.fetch_match_page(session, m.nbbet_slug)
            report["live"] = inspect_raw(parse_match_page(html, m.nbbet_match_id)
                                         .raw_page_tennis_event, m.nbbet_match_id) if html else {"error": "404"}
        except Exception as exc:
            report["live"] = {"error": type(exc).__name__}
        time.sleep(2)
    for (gender, level, year), members in groups.items():
        wanted = {str(m.match_id): m for m in members}
        for kind in ("nbbet_raw", "nbbet_stats"):
            prefix = f"{kind}/gender={gender}/level={level}/year={year}/"
            try:
                keys = sc.list_keys(s3, bucket, prefix)
            except Exception as exc:
                for mid in wanted:
                    result[mid]["scan_errors"].append({"prefix": prefix, "error": type(exc).__name__})
                continue
            for key in keys:
                try:
                    for row in sc.read_rows(s3, bucket, key):
                        mid = str(row.get("canonical_match_id"))
                        if mid not in wanted:
                            continue
                        entry = {"key": key, "fetched_at": row.get("fetched_at")}
                        if kind == "nbbet_raw":
                            try:
                                entry.update(inspect_raw(json.loads(row["raw_json"]), wanted[mid].nbbet_match_id))
                            except Exception as exc:
                                entry["error"] = type(exc).__name__
                            result[mid]["raw"].append(entry)
                        else:
                            entry.update({"scope": row["scope"], **sc.stat_projection(row)})
                            result[mid]["stats"].append(entry)
                except Exception as exc:
                    for mid in wanted:
                        result[mid]["scan_errors"].append({"key": key, "error": type(exc).__name__})
                time.sleep(0.1)
    print(json.dumps({"requested_ids": IDS, "matches": result,
                      "missing_ids": sorted(set(IDS) - {m.nbbet_match_id for m in matches})},
                     ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
