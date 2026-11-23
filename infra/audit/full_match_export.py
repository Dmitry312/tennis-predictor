"""Read-only full-record sample from three Parquet prefixes and public.matches.

Output is a bounded JSON artifact, not stdout. No server-side files are
persisted: caller supplies an already-open output stream.
"""
import datetime as dt
import decimal
import json
import uuid

PREFIXES = ("pbp/", "nbbet_stats/", "nbbet_details/")
MAX_OBJECT = 512 * 1024
MAX_OBJECTS = 60
MAX_MATCHES = 12
MAX_ROWS_PER_MATCH_SOURCE = 1500
MAX_EXPORT_BYTES = 8 * 1024 * 1024


def json_value(value):
    if isinstance(value, (dt.date, dt.datetime, uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return {"binary_bytes": len(value)}
    raise TypeError("unsupported export value")


def select_shared_matches(objects_by_prefix, *, limit=MAX_MATCHES):
    """Select actual IDs present in details; avoid conflating file and match."""
    details = objects_by_prefix.get("nbbet_details/", {})
    return sorted(details)[:limit]


def gather(client, bucket, *, max_keys=200):
    """Read complete small objects, group all rows by canonical match ID."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    found = {prefix: {} for prefix in PREFIXES}
    requests = 0
    for prefix in PREFIXES:
        response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_keys)
        for obj in response.get("Contents", []):
            key, size = obj.get("Key"), obj.get("Size")
            if not isinstance(key, str) or not key.startswith(prefix) or not key.endswith(".parquet"):
                continue
            if type(size) is not int or size < 12 or size > MAX_OBJECT:
                continue
            if requests >= MAX_OBJECTS:
                break
            requests += 1
            body = client.get_object(Bucket=bucket, Key=key)["Body"]
            try:
                payload = body.read(MAX_OBJECT + 1)
            finally:
                body.close()
            if len(payload) != size:
                raise ValueError("object size mismatch")
            parquet = pq.ParquetFile(pa.BufferReader(payload))
            if parquet.metadata.num_rows > 100000 or parquet.metadata.num_columns > 128:
                continue
            if "canonical_match_id" not in parquet.schema_arrow.names:
                continue
            rows = parquet.read().to_pylist()
            for row in rows:
                try:
                    match_id = str(uuid.UUID(str(row.get("canonical_match_id"))))
                except (ValueError, TypeError, AttributeError):
                    continue
                record = found[prefix].setdefault(match_id, {"source_keys": [], "rows": []})
                if key not in record["source_keys"]:
                    record["source_keys"].append(key)
                if len(record["rows"]) < MAX_ROWS_PER_MATCH_SOURCE:
                    record["rows"].append(row)
                else:
                    record["truncated"] = True
    return found


def fetch_pg(connection, ids):
    if not ids:
        return {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT match_id::text, row_to_json(m) FROM public.matches AS m "
            "WHERE match_id = ANY(%s::uuid[]) LIMIT 12", (ids,)
        )
        return {str(key): row for key, row in cursor.fetchall()}


def build_export(objects_by_prefix, pg_rows, *, max_bytes=MAX_EXPORT_BYTES):
    ids = select_shared_matches(objects_by_prefix)
    export = {"version": 1, "complete_history": False, "matches": []}
    for match_id in ids:
        entry = {"match_id": match_id, "postgres": pg_rows.get(match_id),
                 "s3": {prefix: objects_by_prefix.get(prefix, {}).get(match_id)
                        for prefix in PREFIXES}}
        export["matches"].append(entry)
    encoded = json.dumps(export, ensure_ascii=False, default=json_value, allow_nan=False).encode()
    if len(encoded) > max_bytes:
        raise ValueError("export exceeds artifact budget")
    return encoded
