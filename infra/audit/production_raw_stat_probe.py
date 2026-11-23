"""Read-only forensic comparison of original NBBet stats and stored Parquet stats."""
import io
import json
import os
import re
import sys
from pathlib import Path

TARGET_NBBET_ID = 768325
FIELDS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
          "POSTGRES_DB", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
          "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")


def config():
    found = {}
    for line in Path("/root/Tennis_pelmen_model/.env").read_text().splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
        if match and match.group(1) in FIELDS:
            key, value = match.groups()
            found[key] = value.strip("'\"")
    if any(not found.get(k) for k in FIELDS):
        raise ValueError("missing configuration")
    if found["S3_ENDPOINT_URL"] != "https://storage.yandexcloud.net":
        raise ValueError("unexpected endpoint")
    return found


def extract_scopes(raw_json):
    """Source match[25] is [match, set1, set2, ...]; index 3=all, 4=won."""
    event = json.loads(raw_json)
    match = event["match"]
    rows = match["25"]
    return [{"scope": i, "first_serve_all_p2": row[3][1],
             "first_serve_won_p2": row[4][1]} for i, row in enumerate(rows)]


def run():
    import boto3
    import psycopg2
    import pyarrow as pa
    import pyarrow.parquet as pq
    from botocore.config import Config

    c = config()
    with psycopg2.connect(host=c["POSTGRES_HOST"], port=int(c["POSTGRES_PORT"]),
                          user=c["POSTGRES_USER"], password=c["POSTGRES_PASSWORD"],
                          dbname=c["POSTGRES_DB"], connect_timeout=5,
                          options="-c default_transaction_read_only=on -c statement_timeout=5000") as db:
        db.set_session(readonly=True)
        with db.cursor() as cur:
            cur.execute("SELECT match_id::text, pbp_s3_path FROM public.matches "
                        "WHERE nbbet_match_id = %s AND duplicate_of IS NULL "
                        "AND pbp_s3_path IS NOT NULL LIMIT 2", (TARGET_NBBET_ID,))
            matches = cur.fetchall()
    if len(matches) != 1:
        raise ValueError("target match not uniquely identified")
    match_id, pbp_key = matches[0]
    if not re.fullmatch(r"pbp/gender=[MF]/level=[a-z_]+/year=[0-9]{4}/part-[A-Za-z0-9_-]+\.parquet", pbp_key):
        raise ValueError("unexpected partition key")
    suffix = pbp_key.removeprefix("pbp/")
    client = boto3.client("s3", endpoint_url=c["S3_ENDPOINT_URL"],
                          region_name=c["S3_REGION"],
                          aws_access_key_id=c["S3_ACCESS_KEY_ID"],
                          aws_secret_access_key=c["S3_SECRET_ACCESS_KEY"],
                          config=Config(connect_timeout=5, read_timeout=8,
                                        retries={"max_attempts": 1}))
    result = {"nbbet_match_id": TARGET_NBBET_ID, "canonical_match_id": match_id}
    for prefix in ("nbbet_raw/", "nbbet_stats/"):
        key = prefix + suffix
        obj = client.get_object(Bucket=c["S3_BUCKET"], Key=key)
        try:
            payload = obj["Body"].read(2_000_001)
        finally:
            obj["Body"].close()
        if len(payload) > 2_000_000:
            raise ValueError("partition exceeds 2MB")
        rows = pq.read_table(pa.BufferReader(payload)).to_pylist()
        matching = [r for r in rows if str(r.get("canonical_match_id")) == match_id]
        if prefix == "nbbet_raw/":
            if len(matching) != 1 or not matching[0].get("raw_json"):
                raise ValueError("raw row missing or duplicated")
            result["raw_fetched_at"] = str(matching[0].get("fetched_at"))
            result["raw"] = extract_scopes(matching[0]["raw_json"])
        else:
            result["stored"] = [
                {"scope": r["scope"],
                 "first_serve_all_p2": r["first_serve_points_all_p2"],
                 "first_serve_won_p2": r["first_serve_points_won_p2"],
                 "fetched_at": str(r.get("fetched_at"))}
                for r in matching]
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        # Error details do not contain credentials or source payload.
        print("raw_probe_failed:" + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
