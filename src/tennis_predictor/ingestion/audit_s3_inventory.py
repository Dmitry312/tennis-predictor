"""Bounded, read-only inventory of tennis Parquet prefixes in Yandex S3."""
import argparse
import json
from collections import Counter, defaultdict

from tennis_predictor.config import get_settings
from tennis_predictor.storage.s3_client import get_s3_client

PREFIXES = ("pbp/", "nbbet_details/", "nbbet_stats/", "nbbet_raw/")


def summarize_key(key):
    fields = {}
    for part in key.split("/"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k in ("gender", "level", "year"):
                fields[k] = v
    return fields


def audit(s3, bucket, max_objects_per_prefix=10000, max_pages=100, year=None):
    output = {"bucket": bucket, "read_only": True, "prefixes": {}}
    for prefix in PREFIXES:
        totals = Counter()
        partitions = defaultdict(Counter)
        problems = []
        truncated = False
        pages = 0
        scanned = 0
        token = None
        while pages < max_pages and scanned < max_objects_per_prefix:
            kwargs = {"Bucket": bucket, "Prefix": prefix,
                      "MaxKeys": min(1000, max_objects_per_prefix - scanned)}
            if token:
                kwargs["ContinuationToken"] = token
            response = s3.list_objects_v2(**kwargs)
            pages += 1
            items = response.get("Contents", [])
            scanned += len(items)
            for item in items:
                key = item["Key"]
                fields = summarize_key(key)
                if year is not None and fields.get("year") != str(year):
                    continue
                size = int(item["Size"])
                totals["objects"] += 1
                totals["bytes"] += size
                if size == 0:
                    totals["zero_byte"] += 1
                    if len(problems) < 20:
                        problems.append({"key": key, "issue": "zero_byte"})
                if not key.endswith(".parquet"):
                    totals["non_parquet"] += 1
                    if len(problems) < 20:
                        problems.append({"key": key, "issue": "non_parquet"})
                if len(fields) != 3:
                    totals["nonstandard_partition"] += 1
                    if len(problems) < 20:
                        problems.append({"key": key, "issue": "nonstandard_partition"})
                group = (fields.get("year", "unknown"), fields.get("gender", "unknown"),
                         fields.get("level", "unknown"))
                partitions[group]["objects"] += 1
                partitions[group]["bytes"] += size
            token = response.get("NextContinuationToken")
            if not token:
                break
        if token:
            truncated = True
        output["prefixes"][prefix] = {
            "totals": dict(totals), "pages": pages, "scanned": scanned,
            "truncated": truncated,
            "partitions": [{"year": y, "gender": g, "level": l, **dict(v)}
                           for (y, g, l), v in sorted(partitions.items())],
            "sample_problems": problems,
        }
    return output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--year", type=int)
    p.add_argument("--max-objects-per-prefix", type=int, default=10000)
    p.add_argument("--max-pages", type=int, default=100)
    args = p.parse_args()
    if not 1 <= args.max_objects_per_prefix <= 100000 or not 1 <= args.max_pages <= 1000:
        p.error("invalid limits")
    settings = get_settings()
    result = audit(get_s3_client(), settings.s3_bucket,
                   args.max_objects_per_prefix, args.max_pages, args.year)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
