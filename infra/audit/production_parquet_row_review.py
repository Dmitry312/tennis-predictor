"""Bounded read-only *value* sampling for human semantics review.

Run only with the paired manually dispatched SSH workflow. The review sanitizer
is prepended to this script on stdin; no application imports or server writes.
"""
import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path

PREFIXES = ("pbp/", "nbbet_stats/", "nbbet_details/")
MAX_BYTES = 8 * 1024 * 1024
MAX_CALLS = 30
MAX_OUTPUT = 24000
MAX_FILE = 256 * 1024
MAX_LIST = 200
MAX_FILES = 9
MAX_ROWS = 12
FIELDS = ("S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET", "S3_ACCESS_KEY_ID",
          "S3_SECRET_ACCESS_KEY", "POSTGRES_HOST", "POSTGRES_PORT",
          "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")


def read_config():
    # Same strict, non-interpolating environment parser as the metadata audit.
    from pathlib import Path
    raw = Path("/root/Tennis_pelmen_model/.env").read_bytes()
    if len(raw) > 65536:
        raise ValueError("config")
    values = {}
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if not match:
            continue
        key, value = match.groups()
        if key not in FIELDS:
            continue
        if key in values:
            raise ValueError("config")
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#")):
                raise ValueError("config")
            value = value[1:end]
        elif not re.fullmatch(r"[^\s#'\\"\\]+", value):
            raise ValueError("config")
        if not value or len(value) > 4096 or any(x in value for x in ("\\\", "$", "`", "\x00")):
            raise ValueError("config")
        values[key] = value
    if any(not values.get(k) for k in FIELDS):
        raise ValueError("config")
    if values["S3_ENDPOINT_URL"] != "https://storage.yandexcloud.net" or values["S3_REGION"] != "ru-central1":
        raise ValueError("config")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", values["S3_BUCKET"]):
        raise ValueError("config")
    if not re.fullmatch(r"[0-9]{1,5}", values["POSTGRES_PORT"]):
        raise ValueError("config")
    return values


class Budget:
    def __init__(self):
        self.calls = 0
        self.bytes = 0
        self.deadline = time.monotonic() + 65

    def charge(self, size=0):
        if time.monotonic() > self.deadline or self.calls >= MAX_CALLS or self.bytes + size > MAX_BYTES:
            raise ValueError("budget")
        self.calls += 1
        self.bytes += size


def collect(client, bucket, budget):
    """Read at most 3 small objects per prefix, bounded before GET."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    samples = []
    identities = []
    for prefix in PREFIXES:
        budget.charge()
        page = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=MAX_LIST)
        objects = page.get("Contents", [])
        if not isinstance(objects, list) or len(objects) > MAX_LIST:
            raise ValueError("listing")
        candidates = [
            o for o in objects
            if isinstance(o.get("Key"), str)
            and o["Key"].startswith(prefix)
            and o["Key"].endswith(".parquet")
            and type(o.get("Size")) is int and 12 <= o["Size"] <= MAX_FILE
        ]
        # Lexicographic spacing, not random sampling or representative coverage.
        picks = [candidates[i] for i in sorted(set(
            j * (len(candidates) - 1) // max(1, min(3, len(candidates)) - 1)
            for j in range(min(3, len(candidates)))
        ))] if candidates else []
        for item in picks:
            key, size = item["Key"], item["Size"]
            budget.charge(size)
            obj = client.get_object(Bucket=bucket, Key=key)
            stream = obj["Body"]
            try:
                data = stream.read(size + 1)
            finally:
                stream.close()
            if len(data) != size:
                raise ValueError("object_size")
            table = pq.read_table(pa.BufferReader(data), columns=[
                c for c in pq.read_schema(pa.BufferReader(data)).names
                if c in (PBP_INT | PBP_BOOL | STATS_INT | STATS_NUMERIC |
                         DETAIL_INT | DETAIL_BOOL | {"sets_json", "score_server",
                         "score_returner", "point_type", "canonical_match_id"})
            ])
            if table.num_rows > 100000 or table.num_columns > 64:
                raise ValueError("table_size")
            rows = table.slice(0, MAX_ROWS).to_pylist()
            entries = []
            for row in rows:
                raw_id = row.get("canonical_match_id")
                try:
                    identity = str(uuid.UUID(raw_id)) if isinstance(raw_id, str) else None
                except (ValueError, AttributeError):
                    identity = None
                if identity is not None and identity not in identities and len(identities) < 24:
                    identities.append(identity)
                alias = "M" + str(identities.index(identity) + 1) if identity in identities else None
                entries.append({"match": alias, "values": sanitize_row(prefix, row)})
            samples.append({
                "prefix": prefix,
                "schema": sorted({c for c in table.column_names if c in (PBP_INT | PBP_BOOL | STATS_INT | STATS_NUMERIC | DETAIL_INT | DETAIL_BOOL | {"sets_json", "score_server", "score_returner", "point_type"})}),
                "rows_in_file": table.num_rows,
                "examples": entries,
            })
    return samples, identities


def pg_surfaces(values, identities):
    """One transaction, SELECT-only, no names/odds, with timeout and bound IDs."""
    if not identities:
        return {}
    import psycopg2
        connection = psycopg2.connect(
        host=values["POSTGRES_HOST"], port=int(values["POSTGRES_PORT"]),
        user=values["POSTGRES_USER"], password=values["POSTGRES_PASSWORD"],
        dbname=values["POSTGRES_DB"], connect_timeout=3,
        options="-c default_transaction_read_only=on -c statement_timeout=3000",
    )
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT match_id::text, surface FROM matches WHERE match_id = ANY(%s::uuid[]) LIMIT 24",
                (identities,),
            )
            return {key: surface_from_pg(value) for key, value in cursor.fetchall()}
    finally:
        connection.rollback()
        connection.close()


def main():
    import boto3
    from botocore.config import Config
    values = read_config()
    budget = Budget()
    client = boto3.client(
        "s3", endpoint_url=values["S3_ENDPOINT_URL"], region_name=values["S3_REGION"],
        aws_access_key_id=values["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=values["S3_SECRET_ACCESS_KEY"],
        config=Config(connect_timeout=2, read_timeout=3,
                      retries={"mode": "standard", "total_max_attempts": 1},
                      s3={"addressing_style": "path"}),
    )
    # No arbitrary prefixes, buckets, writes, or object ranges.
    def guard(model, params, **kwargs):
        if params.get("Bucket") != values["S3_BUCKET"]:
            raise ValueError("bucket")
        if model.name == "ListObjectsV2":
            if params.get("Prefix") not in PREFIXES or params.get("MaxKeys") != MAX_LIST:
                raise ValueError("list")
        elif model.name == "GetObject":
            if not isinstance(params.get("Key"), str) or not any(
                params["Key"].startswith(p) and params["Key"].endswith(".parquet")
                for p in PREFIXES
            ):
                raise ValueError("key")
        else:
            raise ValueError("operation")
    client.meta.events.register("before-parameter-build.s3", guard)
    samples, identities = collect(client, values["S3_BUCKET"], budget)
    surfaces = pg_surfaces(values, identities)
    report = {
        "version": 1, "semantic_verification": "pending",
        "sampling": "first_200_keys_per_prefix_lexicographic_not_representative",
        "partial": True, "samples": samples,
        "postgres_surfaces": [
            {"match": "M" + str(i + 1), "found": identity in surfaces,
             "surface": surfaces.get(identity)}
            for i, identity in enumerate(identities)
        ],
        "limits": {"max_file_bytes": MAX_FILE, "max_calls": MAX_CALLS,
                   "max_bytes": MAX_BYTES, "max_output_bytes": MAX_OUTPUT},
        "budget": {"calls": budget.calls, "bytes": budget.bytes},
    }
    output = json.dumps(report, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(output.encode()) > MAX_OUTPUT:
        raise ValueError("output_limit")
    print(output)
    return 0


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.alarm(95)
    try:
        sys.exit(main())
    except Exception:
        print('{"version":1,"complete":false,"error":"sample_unavailable"}')
        sys.exit(1)
