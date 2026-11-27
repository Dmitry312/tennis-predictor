"""Pre-11:00 NBBet statistics reconciliation. Dry-run by default.

Checks every eligible completed match in the selected date window, all scopes and
all 14 stat types for both players. Corrections are append-only Parquet versions
under nbbet_stats/, never destructive rewrites of existing partitions.
"""
import argparse
import io
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select

from tennis_predictor.config import get_settings
from tennis_predictor.ingestion.http import make_session
from tennis_predictor.ingestion.nbbet.details import STAT_FIELDS, fetch_match_page, parse_match_page
from tennis_predictor.ingestion.nbbet.discovery import HEADERS
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.models import Match
from tennis_predictor.storage.s3_client import get_s3_client

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Moscow")
FIELDS = tuple(f"{name}_p{player}" for name in STAT_FIELDS.values() for player in (1, 2))
MAX_KEYS_PER_GROUP = 12000


def stat_projection(row):
    return {name: normalize(row.get(name)) for name in FIELDS}


def normalize(value):
    if value is None or (not isinstance(value, (dict, list)) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


def stat_differences(old_rows, new_rows):
    """Compare every scope/field; do not silently erase values missing upstream."""
    old = {int(row["scope"]): stat_projection(row) for row in old_rows}
    new = {int(row["scope"]): stat_projection(row) for row in new_rows}
    if len(old) != len(old_rows) or len(new) != len(new_rows):
        raise ValueError("duplicate statistics scope")
    if not old or not new or old.keys() != new.keys():
        raise ValueError("missing or changed statistics scopes")
    changes = []
    for scope in sorted(old):
        for field in FIELDS:
            before, after = old[scope][field], new[scope][field]
            if before is not None and after is None:
                raise ValueError("source removed existing statistic")
            if before != after:
                changes.append((scope, field, before, after))
    return changes


def validate_changed_stats(rows, differences):
    """Reject inconsistent changed additive counters when all scopes are present."""
    by_scope = {int(row["scope"]): stat_projection(row) for row in rows}
    sets = sorted(scope for scope in by_scope if scope > 0)
    additive = ("aces", "double_faults", "first_serve_points_all",
                "first_serve_points_won", "second_serve_points_all",
                "second_serve_points_won")
    for field in {change[1] for change in differences}:
        if field.rsplit("_p", 1)[0] not in additive:
            continue
        values = [row[field] for row in by_scope.values()]
        if any(v is not None and (not isinstance(v, (int, float)) or
                                  v < 0 or not float(v).is_integer()) for v in values):
            raise ValueError(f"invalid counter: {field}")
        if 0 in by_scope and sets and sets == list(range(1, max(sets) + 1)):
            total = by_scope[0][field]
            parts = [by_scope[scope][field] for scope in sets]
            if total is not None and all(v is not None for v in parts) and total != sum(parts):
                raise ValueError(f"match/set counter mismatch: {field}")


def list_keys(s3, bucket, prefix):
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []) if obj["Key"].endswith(".parquet"))
        if len(keys) > MAX_KEYS_PER_GROUP:
            raise RuntimeError("partition listing budget exceeded")
    return sorted(keys)


def read_rows(s3, bucket, key):
    obj = s3.get_object(Bucket=bucket, Key=key)
    try:
        body = obj["Body"].read(32_000_001)
    finally:
        obj["Body"].close()
    if len(body) > 32_000_000:
        raise RuntimeError("partition size budget exceeded")
    return pq.read_table(pa.BufferReader(body)).to_pylist()


def latest_rows(rows):
    """Select the newest version for each match/scope, retaining full rows."""
    result = {}
    for row in rows:
        key = (str(row["canonical_match_id"]), int(row["scope"]))
        stamp = str(row.get("fetched_at") or "")
        if key not in result or stamp > result[key][0]:
            result[key] = (stamp, row)
    return result


def load_group(s3, bucket, gender, level, year, matches):
    """Read original PBP-linked partitions plus later crosscheck versions.

    For matches without PBP pointer, locate their stats by scanning the group's
    partition keys. No match is silently skipped.
    """
    prefix = f"nbbet_stats/gender={gender}/level={level}/year={year}/"
    keys = list_keys(s3, bucket, prefix)
    original = set()
    needs_discovery = any(not m.pbp_s3_path for m in matches)
    for m in matches:
        if m.pbp_s3_path:
            original.add("nbbet_stats/" + m.pbp_s3_path.removeprefix("pbp/"))
    # Crosscheck keys are separate from original partitions; older records
    # remain immutable, and newest fetched_at wins on future runs.
    selected = set(keys if needs_discovery else
                   [k for k in keys if "-crosscheck-" in k] + list(original))
    if not needs_discovery:
        selected |= original
    wanted = {str(m.match_id) for m in matches}
    rows = []
    for key in sorted(selected):
        rows.extend(row for row in read_rows(s3, bucket, key)
                    if str(row['canonical_match_id']) in wanted)
    return latest_rows(rows)


def fetch_candidates(day, lookback):
    start = day - timedelta(days=lookback - 1)
    with engine.connect() as conn:
        return conn.execute(
            select(Match.match_id, Match.nbbet_match_id, Match.nbbet_slug,
                   Match.gender, Match.level, Match.match_date, Match.pbp_s3_path)
            .where(Match.match_date >= start, Match.match_date <= day,
                   Match.status.in_((3, 9, 10)),
                   Match.details_fetched_at.is_not(None),
                   Match.nbbet_slug.is_not(None), Match.duplicate_of.is_(None))
            .order_by(Match.match_date, Match.match_id)
        ).all()


def reconcile(*, day, lookback, apply, max_matches, pause, deadline, allow_partial=False):
    candidates = fetch_candidates(day, lookback)
    if len(candidates) > max_matches:
        raise RuntimeError(f"candidate count {len(candidates)} exceeds budget {max_matches}")
    s3, bucket = get_s3_client(), get_settings().s3_bucket
    session = make_session(headers=HEADERS, retry_statuses=(500, 502, 503, 504))
    groups = defaultdict(list)
    for m in candidates:
        groups[(m.gender, m.level, m.match_date.year)].append(m)
    summary = dict(candidates=len(candidates), checked=0, unchanged=0,
                   changed=0, missing_stats=0, failures=0, fields_changed=0, written=0)
    issues = []
    detailed_changes = []
    pending = []
    consecutive_failures = 0
    for (gender, level, year), matches in sorted(groups.items()):
        stored = load_group(s3, bucket, gender, level, year, matches)
        by_match = defaultdict(list)
        for (mid, _), (_, row) in stored.items():
            by_match[mid].append(row)
        corrections = []
        raw_corrections = []
        for m in matches:
            if time.monotonic() >= deadline:
                raise TimeoutError("crosscheck deadline exceeded before 11:00")
            mid = str(m.match_id)
            old = by_match[mid]
            if not old:
                summary["missing_stats"] += 1
                issues.append({"match_id": mid, "reason": "missing_stats"})
                continue
            try:
                html = fetch_match_page(session, m.nbbet_slug)
                if html is None:
                    raise ValueError("source page unavailable")
                parsed = parse_match_page(html, m.nbbet_match_id)
                fresh = [{"canonical_match_id": mid, **row} for row in parsed.stats]
                differences = stat_differences(old, fresh)
                if differences:
                    validate_changed_stats(fresh, differences)
                summary["checked"] += 1
                consecutive_failures = 0
                if differences:
                    summary["changed"] += 1
                    summary["fields_changed"] += len(differences)
                    log.info("stats_changed match=%s fields=%d", mid, len(differences))
                    detailed_changes.append({"canonical_match_id": mid,
                                             "nbbet_match_id": m.nbbet_match_id,
                                             "match_date": str(m.match_date),
                                             "gender": m.gender, "level": m.level,
                                             "changes": [{"scope": scope, "field": field,
                                                          "before": before, "after": after}
                                                         for scope, field, before, after in differences]})
                    corrections.extend(fresh)
                    raw_corrections.append({"canonical_match_id": mid,
                                            "nbbet_match_id": m.nbbet_match_id,
                                            "raw_json": json.dumps(parsed.raw_page_tennis_event, ensure_ascii=False)})
                else:
                    summary["unchanged"] += 1
            except Exception as exc:
                summary["failures"] += 1
                issues.append({"match_id": mid, "reason": type(exc).__name__})
                consecutive_failures += 1
                log.warning("stats_check_failed match=%s reason=%s", mid, type(exc).__name__)
                if consecutive_failures >= 3:
                    raise RuntimeError("three consecutive source/check failures: stopping") from exc
            time.sleep(pause)
        if corrections:
            pending.append((gender, level, year, corrections, raw_corrections))
    # Never publish partial audit results.
    summary['issues'] = issues
    summary['complete'] = not issues
    if issues and not allow_partial:
        print(json.dumps(summary, sort_keys=True))
        raise RuntimeError('crosscheck incomplete: no corrections published')
    if apply:
        for gender, level, year, corrections, raw_corrections in pending:
            stamp = datetime.utcnow()
            prefix = f'gender={gender}/level={level}/year={year}/'
            name = f'part-{stamp:%Y%m%dT%H%M%S%f}-crosscheck-{len(corrections)}.parquet'
            for row in corrections + raw_corrections:
                row['fetched_at'] = stamp
            # Archive the exact fetched payload before publishing its statistics.
            for kind, rows in (('nbbet_raw', raw_corrections),
                               ('nbbet_stats', corrections)):
                buffer = io.BytesIO()
                pq.write_table(pa.Table.from_pylist(rows), buffer, compression='zstd')
                buffer.seek(0)
                s3.put_object(Bucket=bucket, Key=f'{kind}/{prefix}{name}',
                              Body=buffer, IfNoneMatch='*')
            summary['written'] += len(corrections)
    summary['details'] = detailed_changes
    print(json.dumps(summary, sort_keys=True, default=str))
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="End date YYYY-MM-DD; default yesterday in Moscow")
    p.add_argument("--lookback-days", type=int, default=3)
    p.add_argument("--max-matches", type=int, default=300)
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--max-minutes", type=int, default=40)
    p.add_argument("--apply", action="store_true", help="Write changed statistics as new S3 versions")
    args = p.parse_args()
    if not 1 <= args.lookback_days <= 30 or not 1 <= args.max_matches <= 5000:
        p.error("invalid lookback or match budget")
    if args.pause < 0.5 or not 1 <= args.max_minutes <= 60:
        p.error("invalid rate or time budget")
    end = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else (
        datetime.now(TZ).date() - timedelta(days=1))
    logging.basicConfig(level=logging.INFO)
    reconcile(day=end, lookback=args.lookback_days, apply=args.apply,
              max_matches=args.max_matches, pause=args.pause,
              deadline=time.monotonic() + args.max_minutes * 60)


if __name__ == "__main__":
    main()
