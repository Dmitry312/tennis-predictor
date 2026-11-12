"""Read-only, bounded point-by-point quality audit.

Usage: python -m tennis_predictor.ingestion.audit_pbp --days 7
No writes to PostgreSQL or S3. Output is JSON to stdout.
"""
import argparse
import io
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select

from tennis_predictor.config import get_settings
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.models import Match
from tennis_predictor.storage.s3_client import get_s3_client

MOSCOW = ZoneInfo("Europe/Moscow")


def inspect_match(frame: pd.DataFrame) -> dict:
    """Detect structural anomalies; do not infer missing points from contiguous indices."""
    if frame.empty:
        return {"status": "missing", "points": 0, "games": 0, "issues": ["match_not_in_parquet"]}
    issues = []
    if frame.duplicated(["set_num", "game_num", "point_num"]).any():
        issues.append("duplicate_point_index")
    if frame["point_winner"].isna().any():
        issues.append("unresolved_point_winner")
    incomplete = []
    for (s, g), group in frame.groupby(["set_num", "game_num"], dropna=False):
        nums = sorted(set(group["point_num"].dropna().astype(int)))
        if not nums or nums != list(range(1, max(nums) + 1)):
            issues.append(f"point_index_gap:{s}:{g}")
        if not group["game_complete"].fillna(False).all():
            incomplete.append([int(s), int(g)])
    for s, group in frame.groupby("set_num"):
        nums = sorted(set(group["game_num"].dropna().astype(int)))
        if not nums or nums != list(range(1, max(nums) + 1)):
            issues.append(f"game_index_gap:{s}")
    if incomplete:
        issues.append("incomplete_game_requires_score_and_status_review")
    return {
        "status": "review" if issues else "structurally_ok",
        "points": int(len(frame)),
        "games": int(frame.groupby(["set_num", "game_num"]).ngroups),
        "incomplete_games": incomplete,
        "issues": sorted(set(issues)),
    }


def run(days: int, max_matches: int, max_object_bytes: int) -> dict:
    if not 1 <= days <= 31 or not 1 <= max_matches <= 2000:
        raise ValueError("days must be 1..31 and max_matches 1..2000")
    today = datetime.now(MOSCOW).date()
    start_local = datetime.combine(today - timedelta(days=days), datetime.min.time(), MOSCOW)
    end_local = datetime.combine(today, datetime.min.time(), MOSCOW)
    start = start_local.astimezone(UTC).replace(tzinfo=None)
    end = end_local.astimezone(UTC).replace(tzinfo=None)
    with engine.connect() as conn:
        rows = conn.execute(
            select(Match.match_id, Match.nbbet_match_id, Match.status,
                   Match.tournament_name, Match.pbp_s3_path, Match.pbp_completeness)
            .where(Match.duplicate_of.is_(None),
                   Match.status.in_([3, 9, 10]),
                   Match.start_ts >= start, Match.start_ts < end)
            .order_by(Match.start_ts, Match.match_id)
            .limit(max_matches + 1)
        ).all()
    if len(rows) > max_matches:
        raise ValueError(f"more than {max_matches} matches; narrow period or raise --max-matches")
    paths = defaultdict(list)
    report = {"period_moscow": [str(start_local.date()), str(end_local.date())],
              "matches": len(rows), "files_requested": 0, "results": []}
    for r in rows:
        if r.pbp_s3_path:
            paths[r.pbp_s3_path].append(r)
        else:
            report["results"].append({"match_id": str(r.match_id), "nbbet_id": r.nbbet_match_id,
                                      "status": "missing", "issues": ["missing_pbp_path"]})
    s3 = get_s3_client()
    bucket = get_settings().s3_bucket
    for path, members in paths.items():
        try:
            meta = s3.head_object(Bucket=bucket, Key=path)
            if meta["ContentLength"] > max_object_bytes:
                raise ValueError("object_exceeds_size_limit")
            raw = s3.get_object(Bucket=bucket, Key=path)["Body"].read()
            report["files_requested"] += 1
            df = pd.read_parquet(io.BytesIO(raw))
            if not {"canonical_match_id", "set_num", "game_num", "point_num",
                    "point_winner", "game_complete"}.issubset(df.columns):
                raise ValueError("missing_required_columns")
            grouped = {str(k): g for k, g in df.groupby(df["canonical_match_id"].astype(str))}
            for r in members:
                result = inspect_match(grouped.get(str(r.match_id), df.iloc[0:0]))
                report["results"].append({"match_id": str(r.match_id),
                                          "nbbet_id": r.nbbet_match_id,
                                          "tournament": r.tournament_name,
                                          "db_completeness": r.pbp_completeness, **result})
        except Exception as exc:
            for r in members:
                report["results"].append({"match_id": str(r.match_id),
                                          "nbbet_id": r.nbbet_match_id,
                                          "status": "error", "issues": [type(exc).__name__ + ": " + str(exc)[:120]]})
    counts = defaultdict(int)
    for result in report["results"]:
        counts[result["status"]] += 1
    report["summary"] = dict(sorted(counts.items()))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--max-matches", type=int, default=1000)
    parser.add_argument("--max-object-mb", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(run(args.days, args.max_matches, args.max_object_mb * 1024**2),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
