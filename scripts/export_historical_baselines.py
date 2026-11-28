"""Bounded, read-only historical export over the protected SSH runtime."""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import UTC
from hashlib import file_digest
from io import BytesIO
from itertools import islice
from pathlib import Path
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.ml.baselines import START
from tennis_predictor.ml.canonical_layer.evidence import encoded, fingerprint
from tennis_predictor.ml.identity_review.pg_export import (
    FIELDS,
    MAX_FRAME,
    connection_factory,
    frame,
    iso,
)

DETAIL_FIELDS = ("canonical_match_id", "nbbet_match_id", "status", "sets_json", "fetched_at")
META_FIELDS = (*FIELDS, "surface")
PAGE = 512
MAX_MATCHES = 500_000
MAX_OBJECTS = 12_000
MAX_OBJECT_BYTES = 32 << 20
MAX_TOTAL_BYTES = 4 << 30
MAX_DETAILS = 1_600_000


STAGES = {"bootstrap", "imports", "registry", "postgres", "inventory", "details"}
ERRORS = {
    "ImportError",
    "ModuleNotFoundError",
    "ClientError",
    "OperationalError",
    "ProgrammingError",
    "QueryCanceled",
    "UndefinedColumn",
    "ArrowInvalid",
    "ValueError",
    "OSError",
    "FileNotFoundError",
    "PermissionError",
    "TimeoutError",
    "EndpointConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
}


def checksum(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def metadata(connect, stream):
    """One read-only repeatable-read cursor, with time and row bounds."""
    deadline = monotonic() + 300
    with closing(connect()) as db:
        db.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
        with db, db.cursor() as setup:
            setup.execute("SET LOCAL statement_timeout = '8s'")
            setup.execute("SET LOCAL lock_timeout = '500ms'")
            setup.execute("SET LOCAL idle_in_transaction_session_timeout = '15s'")
            setup.execute("SELECT transaction_timestamp(), txid_current_snapshot()::text")
            received, tx_snapshot = setup.fetchone()
            as_of = received.astimezone(UTC)
            frame({"postgres": {"as_of": as_of.isoformat(), "transaction": tx_snapshot}}, stream)
            columns = ", ".join(META_FIELDS)
            with db.cursor(name="historical_baseline") as query:
                query.execute(
                    f"SELECT {columns} FROM public.matches "
                    "WHERE match_date >= %s AND match_date <= %s ORDER BY match_id LIMIT %s",
                    (START, as_of.date(), MAX_MATCHES + 1),
                )
                count = 0
                while rows := query.fetchmany(PAGE):
                    count += len(rows)
                    if count > MAX_MATCHES or monotonic() > deadline:
                        raise ValueError("PostgreSQL export budget exceeded")
                    frame(
                        {
                            "metadata": [
                                dict(zip(META_FIELDS, map(iso, row), strict=True)) for row in rows
                            ]
                        },
                        stream,
                    )
    return as_of


def inventory(client, bucket, as_of):
    for gender in ("M", "F"):
        for level in ("tour", "challenger"):
            for year in range(START.year, as_of.year + 1):
                prefix = f"nbbet_details/gender={gender}/level={level}/year={year}/"
                pages = client.get_paginator("list_objects_v2").paginate(
                    Bucket=bucket, Prefix=prefix
                )
                for page in pages:
                    for item in page.get("Contents", ()):
                        if item["Key"].endswith(".parquet"):
                            yield item["Key"], item["ETag"], item["Size"]


def details(client, bucket, objects, stream):
    import pyarrow.parquet as pq

    seen = set()
    total_bytes = count = 0
    deadline = monotonic() + 1000
    for key, etag, size in objects:
        total_bytes += size
        if (
            key in seen
            or not key.startswith("nbbet_details/")
            or not 0 < size <= MAX_OBJECT_BYTES
            or total_bytes > MAX_TOTAL_BYTES
            or len(seen) >= MAX_OBJECTS
            or monotonic() > deadline
        ):
            raise ValueError("Details inventory exceeded its bounds")
        seen.add(key)
        response = client.get_object(Bucket=bucket, Key=key, IfMatch=etag)
        with closing(response["Body"]) as body:
            raw = body.read(size + 1)
        if response.get("ETag") != etag or len(raw) != size:
            raise ValueError("Details object changed after inventory")
        parquet = pq.ParquetFile(BytesIO(raw))
        if not set(DETAIL_FIELDS) <= set(parquet.schema_arrow.names):
            raise ValueError("Details object lacks expected result provenance")
        frame({"object": [key, etag, size]}, stream)
        for batch in parquet.iter_batches(batch_size=PAGE, columns=DETAIL_FIELDS):
            count += batch.num_rows
            if count > MAX_DETAILS:
                raise ValueError("Details row budget exceeded")
            frame(
                {"details": [{k: iso(v) for k, v in row.items()} for row in batch.to_pylist()]},
                stream,
            )
        sleep(0.03)


def remote(stream):
    stage = "imports"
    frame({"stage": stage}, stream)
    try:
        import psycopg2  # noqa: F401 -- verify the remote PostgreSQL runtime before reading
        import pyarrow.parquet  # noqa: F401 -- verify the remote Parquet runtime before reading
        from dotenv import dotenv_values  # noqa: F401 -- used by the connection factory

        from tennis_predictor.config import get_settings
        from tennis_predictor.ml.identity_review.publication import RegistryPublication
        from tennis_predictor.storage.s3_client import get_s3_client

        stage = "registry"
        frame({"stage": stage}, stream)
        client, bucket = get_s3_client(), get_settings().s3_bucket
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "registry.sqlite"
            pin = RegistryPublication(client, bucket).fetch(path)
            frame({"registry": {key: pin[key] for key in ("token", "revision", "sha256")}}, stream)
            with path.open("rb") as registry:
                while block := registry.read(256 << 10):
                    frame({"registry_block": base64.b64encode(block).decode()}, stream)
        stage = "postgres"
        frame({"stage": stage}, stream)
        connect, _ = connection_factory()
        as_of = metadata(connect, stream)
        stage = "inventory"
        frame({"stage": stage}, stream)
        objects = sorted(islice(inventory(client, bucket, as_of), MAX_OBJECTS + 1))
        if len(objects) > MAX_OBJECTS:
            raise ValueError("Details inventory exceeds object budget")
        stage = "details"
        frame({"stage": stage}, stream)
        details(client, bucket, objects, stream)
        frame({"done": True}, stream)
        return 0
    except Exception as exc:  # noqa: BLE001 -- never forward a remote error message or raw rows
        name = type(exc).__name__
        frame({"error": name if name in ERRORS else "OtherError"}, stream)
        return 1


def exit_status(process):
    try:
        return str(process.wait(timeout=15))
    except subprocess.TimeoutExpired:
        return "running"


def receive(process, output):
    files = {
        "metadata": "metadata.jsonl",
        "details": "details.jsonl",
        "registry_block": "registry.sqlite",
    }
    source = {}
    objects = []
    stage = "bootstrap"
    counts = dict.fromkeys(files, 0)
    with (
        (output / files["metadata"]).open("xb") as meta,
        (output / files["details"]).open("xb") as detail,
        (output / files["registry_block"]).open("xb") as registry,
    ):
        while True:
            line = process.stdout.readline(MAX_FRAME + 1)
            if not line:
                raise ValueError(f"Remote export EOF at {stage}; exit={exit_status(process)}")
            if len(line) > MAX_FRAME:
                raise ValueError(f"Oversized remote export frame at {stage}")
            if not line.endswith(b"\n"):
                raise ValueError(
                    f"Truncated remote export frame at {stage}; exit={exit_status(process)}"
                )
            try:
                message = json.loads(line)
                ((key, value),) = message.items()
            except (ValueError, AttributeError):
                raise ValueError(f"Invalid remote export frame at {stage}") from None
            if key == "stage" and isinstance(value, str) and value in STAGES:
                stage = value
            elif key == "error" and isinstance(value, str) and value in ERRORS | {"OtherError"}:
                raise ValueError(
                    f"Remote export failed at {stage}: {value}; exit={exit_status(process)}"
                )
            elif key in ("registry", "postgres"):
                if key in source:
                    raise ValueError("Repeated source pin")
                source[key] = value
            elif key == "object":
                objects.append(value)
                if len(objects) > MAX_OBJECTS:
                    raise ValueError("Too many details objects")
            elif key == "registry_block":
                block = base64.b64decode(value, validate=True)
                counts[key] += len(block)
                if counts[key] > 128 << 20:
                    raise ValueError("Registry too large")
                registry.write(block)
            elif key in ("metadata", "details"):
                counts[key] += len(value)
                if counts[key] > (MAX_MATCHES if key == "metadata" else MAX_DETAILS):
                    raise ValueError("Too many input rows")
                for row in value:
                    frame(row, meta if key == "metadata" else detail)
            elif key == "done" and value is True:
                break
            else:
                raise ValueError("Invalid remote export frame")
    status = exit_status(process)
    if status != "0" or set(source) != {"registry", "postgres"} or not all(counts.values()):
        raise ValueError(f"Incomplete source export at {stage}; exit={status}")
    checksums = {name: checksum(output / name) for name in files.values()}
    if checksums["registry.sqlite"] != source["registry"]["sha256"]:
        raise ValueError("Registry changed in transit")
    (output / "source.json").write_bytes(
        encoded(
            {
                "format": "historical-source-v1",
                "export_code": checksum(Path(__file__)),
                "as_of": source["postgres"]["as_of"],
                "postgres_snapshot": source["postgres"]["transaction"],
                "objects": len(objects),
                "details_inventory_fingerprint": fingerprint(objects),
                "registry_revision": source["registry"]["revision"],
                "registry_token": source["registry"]["token"],
                "files": checksums,
                "counts": counts,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    if args.remote:
        raise SystemExit(remote(sys.stdout.buffer))
    from scripts.export_live_player_review import ssh_start

    if args.output is None:
        parser.error("Output directory required")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    payload = sys.stdin.buffer.read(64 << 20)
    if not payload:
        raise ValueError("Missing pinned source archive")
    process = ssh_start(payload, remote_command=os.environ["BASELINE_SSH_COMMAND"])
    try:
        receive(process, args.output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


if __name__ == "__main__":
    main()
