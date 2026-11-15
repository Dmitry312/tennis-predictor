"""Read-only Parquet content audit, bounded by year, object size and file count.

Run from project root with PYTHONPATH=src and configured S3 credentials.
No DB access, no S3 writes, no temporary output files.
"""
import argparse
import io
import json
from collections import Counter, defaultdict

import pyarrow.parquet as pq

from tennis_predictor.config import get_settings
from tennis_predictor.storage.s3_client import get_s3_client

KINDS = {
    "nbbet_details": ["canonical_match_id", "nbbet_match_id", "status", "sets_json", "has_coverage"],
    "nbbet_stats": ["canonical_match_id", "scope"],
    "pbp": ["canonical_match_id", "set_num", "game_num", "point_num", "point_winner", "game_complete"],
    "nbbet_raw": ["canonical_match_id", "nbbet_match_id"],
}


def list_year_keys(s3, bucket, kind, year, max_files):
    """List prefix metadata only; do not assume year is a leading S3 partition."""
    keys = []
    token = None
    while True:
        opts = {"Bucket": bucket, "Prefix": kind + "/", "MaxKeys": 1000}
        if token:
            opts["ContinuationToken"] = token
        result = s3.list_objects_v2(**opts)
        for obj in result.get("Contents", []):
            key = obj["Key"]
            if f"/year={year}/" in key:
                keys.append((key, int(obj["Size"])))
                if len(keys) > max_files:
                    raise ValueError(f"{kind}: more than {max_files} objects in year {year}")
        token = result.get("NextContinuationToken")
        if not token:
            break
    return keys


def audit(s3, bucket, year, max_files, max_object_mb, max_issues):
    by_kind = {}
    issues = []
    sets = {}
    stats_scope0 = set()
    pbp_ids = set()
    raw_ids = set()
    for kind, cols in KINDS.items():
        keys = list_year_keys(s3, bucket, kind, year, max_files)
        counters = Counter(objects=len(keys))
        ids = set()
        for key, size in keys:
            if size == 0 or size > max_object_mb * 1024**2:
                counters["skipped_size"] += 1
                if len(issues) < max_issues:
                    issues.append({"kind": kind, "key": key, "issue": "zero_or_oversize"})
                continue
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                pf = pq.ParquetFile(io.BytesIO(body))
                missing = set(cols) - set(pf.schema_arrow.names)
                if missing:
                    raise ValueError("missing columns: " + ",".join(sorted(missing)))
                # Column projection avoids loading large raw_json or other payload columns.
                table = pf.read(columns=cols)
                data = table.to_pydict()
                counters["readable_objects"] += 1
                counters["rows"] += table.num_rows
                ids_in_file = set()
                for i, match_id in enumerate(data["canonical_match_id"]):
                    if match_id is None or not str(match_id).strip():
                        counters["null_match_id_rows"] += 1
                        continue
                    mid = str(match_id)
                    ids.add(mid)
                    ids_in_file.add(mid)
                    if kind == "nbbet_details":
                        value = data["sets_json"][i]
                        if value is None:
                            counters["null_sets_json"] += 1
                        else:
                            try:
                                parsed = json.loads(value)
                                if not isinstance(parsed, list):
                                    raise ValueError("not a list")
                            except (ValueError, TypeError):
                                counters["invalid_sets_json"] += 1
                        # Multiple versions are expected; do not count as duplicate matches.
                    elif kind == "nbbet_stats":
                        if data["scope"][i] == 0:
                            stats_scope0.add(mid)
                    elif kind == "pbp":
                        if data["point_winner"][i] is None:
                            counters["unresolved_point_winner"] += 1
                        if data["game_complete"][i] is False:
                            counters["points_in_incomplete_games"] += 1
                        if any(data[c][i] is None for c in ("set_num", "game_num", "point_num")):
                            counters["null_point_index"] += 1
                counters["match_file_occurrences"] += len(ids_in_file)
            except Exception as exc:
                counters["unreadable_objects"] += 1
                if len(issues) < max_issues:
                    issues.append({"kind": kind, "key": key,
                                   "issue": type(exc).__name__ + ": " + str(exc)[:150]})
        counters["unique_matches"] = len(ids)
        by_kind[kind] = {"counts": dict(sorted(counters.items())), "ids": ids}
        if kind == "nbbet_details":
            sets = ids
        elif kind == "pbp":
            pbp_ids = ids
        elif kind == "nbbet_raw":
            raw_ids = ids
    detail_ids = by_kind["nbbet_details"]["ids"]
    stat_ids = by_kind["nbbet_stats"]["ids"]
    report = {
        "year": year, "bucket": bucket, "read_only": True,
        "prefixes": {kind: value["counts"] for kind, value in by_kind.items()},
        "cross_dataset": {
            "details_without_stats": len(detail_ids - stat_ids),
            "details_without_stats_scope0": len(detail_ids - stats_scope0),
            "details_without_pbp": len(detail_ids - pbp_ids),
            "details_without_raw": len(detail_ids - raw_ids),
            "stats_without_details": len(stat_ids - detail_ids),
            "pbp_without_details": len(pbp_ids - detail_ids),
            "raw_without_details": len(raw_ids - detail_ids),
        },
        "samples": {
            "details_without_stats": sorted(detail_ids - stat_ids)[:max_issues],
            "details_without_pbp": sorted(detail_ids - pbp_ids)[:max_issues],
            "stats_without_details": sorted(stat_ids - detail_ids)[:max_issues],
            "pbp_without_details": sorted(pbp_ids - detail_ids)[:max_issues],
        },
        "file_issues": issues,
        "limitations": [
            "No PostgreSQL or upstream source reconciliation",
            "Missing stats/pbp can be legitimate; do not classify as corruption automatically",
            "Repeated versions across Parquet files are not deduplicated",
            "Point-level completeness cannot be inferred from contiguous indices",
            "Counts may be incomplete if objects were skipped or unreadable",
        ],
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--max-files", type=int, default=2000)
    parser.add_argument("--max-object-mb", type=int, default=5)
    parser.add_argument("--max-issues", type=int, default=30)
    args = parser.parse_args()
    if not 2010 <= args.year <= 2100 or not 1 <= args.max_files <= 10000:
        parser.error("invalid year or max-files")
    if not 1 <= args.max_object_mb <= 100 or not 1 <= args.max_issues <= 100:
        parser.error("invalid size or sample limit")
    settings = get_settings()
    print(json.dumps(audit(get_s3_client(), settings.s3_bucket, args.year,
                           args.max_files, args.max_object_mb, args.max_issues),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
