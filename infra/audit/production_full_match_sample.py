"""Production read-only sample driver. Emits JSON only to redirected runner artifact."""
import json
import os
import re
import sys
from pathlib import Path


class SampleStageError(Exception):
    """A pre-approved diagnostic code; never includes exception details."""


def stage(name, operation):
    try:
        return operation()
    except Exception:
        raise SampleStageError(name) from None

FIELDS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
          "POSTGRES_DB", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
          "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")


def config():
    raw = Path("/root/Tennis_pelmen_model/.env").read_text()
    if len(raw) > 65536:
        raise ValueError("config limit")
    found = {}
    for line in raw.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
        if not match or match.group(1) not in FIELDS:
            continue
        key, value = match.groups()
        if key in found:
            raise ValueError("duplicate config key")
        if value.startswith(("'", '"')) and value.endswith(value[0]):
            value = value[1:-1]
        if not value or len(value) > 4096 or any(x in value for x in ("\n", "\r", "`", "$", "\\")):
            raise ValueError("invalid config value")
        found[key] = value
    if any(not found.get(key) for key in FIELDS):
        raise ValueError("missing config")
    if found["S3_ENDPOINT_URL"] != "https://storage.yandexcloud.net" or found["S3_REGION"] != "ru-central1":
        raise ValueError("unexpected endpoint")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", found["S3_BUCKET"]):
        raise ValueError("invalid bucket")
    return found


def main():
    # Keep all error messages independent of config, identities, and DB/S3 errors.
    def imports():
        import boto3
        import psycopg2
        from botocore.config import Config
        return boto3, psycopg2, Config

    boto3, psycopg2, Config = stage("dependency_unavailable", imports)
    settings = stage("connection_config_unavailable", config)

    def query_pg():
        db = psycopg2.connect(
            host=settings["POSTGRES_HOST"], port=int(settings["POSTGRES_PORT"]),
            user=settings["POSTGRES_USER"], password=settings["POSTGRES_PASSWORD"],
            dbname=settings["POSTGRES_DB"], connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000 "
                    "-c lock_timeout=1000 -c search_path=pg_catalog",
        )
        try:
            db.set_session(readonly=True, autocommit=False)
            candidates = select_pg_candidates(db)
            ids = [mid for mid, _ in candidates]
            pg_rows = fetch_pg(db, ids)
            return candidates, ids, pg_rows
        finally:
            db.rollback()
            db.close()

    candidates, ids, pg_rows = stage("postgres_select_failed", query_pg)

    def query_s3():
        s3 = boto3.client(
            "s3", endpoint_url=settings["S3_ENDPOINT_URL"],
            region_name=settings["S3_REGION"],
            aws_access_key_id=settings["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=settings["S3_SECRET_ACCESS_KEY"],
            config=Config(connect_timeout=5, read_timeout=8, retries={"max_attempts": 1}),
        )
        keys = sorted({(prefix, path) for _, mapping in candidates
                       for prefix, path in mapping.items()})
        return gather_exact_keys(s3, settings["S3_BUCKET"], keys)

    data = stage("s3_partition_read_failed", query_s3)
    output = stage("sample_serialization_failed",
                   lambda: build_export(data, pg_rows, match_ids=ids))
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    try:
        main()
    except SampleStageError as error:
        print("sample_stage:" + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("sample_stage:unexpected_failure", file=sys.stderr)
        sys.exit(1)
