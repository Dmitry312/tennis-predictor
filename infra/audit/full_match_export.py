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
MAX_PAGES_PER_PREFIX = 10
MAX_MATCHES = 12
MAX_ROWS_PER_MATCH_SOURCE = 1500
MAX_EXPORT_BYTES = 8 * 1024 * 1024


def json_value(value):
    if isinstance(value, (dt.date, dt.datetime, uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, bytes):
        import base64
        return {"binary_base64": base64.b64encode(value).decode("ascii")}
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
        token = None
        pages = 0
        while True:
            if pages >= MAX_PAGES_PER_PREFIX:
                raise ValueError("S3 listing incomplete: page budget")
            kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": max_keys}
            if token:
                kwargs["ContinuationToken"] = token
            response = client.list_objects_v2(**kwargs)
            pages += 1
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
                        raise ValueError("match source exceeds row budget")
            if requests >= MAX_OBJECTS:
                raise ValueError("S3 object budget exhausted before complete scan")
            if not response.get("IsTruncated", False):
                break
            token = response.get("NextContinuationToken")
            if not isinstance(token, str) or not token:
                raise ValueError("S3 listing incomplete: missing continuation token")
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


def build_export(objects_by_prefix, pg_rows, *, max_bytes=MAX_EXPORT_BYTES, match_ids=None):
    ids = list(match_ids) if match_ids is not None else select_shared_matches(objects_by_prefix)
    if len(ids) > MAX_MATCHES or len(set(ids)) != len(ids):
        raise ValueError("invalid match selection")
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


def related_partition_keys(pbp_s3_path):
    """NBBET detail_job writes all three datasets under the same part suffix.

    This is a candidate lookup, not proof that each object exists.
    """
    import re
    if not isinstance(pbp_s3_path, str):
        return None
    match = re.fullmatch(
        r"pbp/(gender=[MF]/level=[a-z_]+/year=[0-9]{4}/part-[A-Za-z0-9_-]+\.parquet)",
        pbp_s3_path,
    )
    if not match:
        return None
    part = match.group(1)
    return {prefix: prefix + part for prefix in PREFIXES}


def gather_exact_keys(client, bucket, keys):
    """Read every row from exact known partition objects, without LIST scans."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    result = {prefix: {} for prefix in PREFIXES}
    if len(keys) > MAX_OBJECTS:
        raise ValueError("too many partition objects")
    for prefix, key in keys:
        if prefix not in PREFIXES or not isinstance(key, str) or not key.startswith(prefix):
            raise ValueError("invalid partition")
        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                continue
            raise
        size = head.get("ContentLength")
        if type(size) is not int or not 12 <= size <= MAX_OBJECT:
            raise ValueError("partition size unsupported")
        stream = client.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            payload = stream.read(size + 1)
        finally:
            stream.close()
        if len(payload) != size:
            raise ValueError("partition length mismatch")
        parquet = pq.ParquetFile(pa.BufferReader(payload))
        if parquet.metadata.num_rows > 100000 or parquet.metadata.num_columns > 128:
            raise ValueError("partition row/column limit")
        for row in parquet.read().to_pylist():
            try:
                mid = str(uuid.UUID(str(row.get("canonical_match_id"))))
            except (ValueError, TypeError, AttributeError):
                continue
            record = result[prefix].setdefault(mid, {"source_keys": [], "rows": []})
            if key not in record["source_keys"]:
                record["source_keys"].append(key)
            if len(record["rows"]) >= MAX_ROWS_PER_MATCH_SOURCE:
                raise ValueError("match row budget")
            record["rows"].append(row)
    return result


class CandidateReadError(Exception):
    """Safe stage identifier only; never includes database values."""


def select_pg_candidates(connection, *, limit=MAX_MATCHES):
    """Prefer known PBP paths; return canonical IDs and exact object candidates."""
    if type(limit) is not int or not 1 <= limit <= MAX_MATCHES:
        raise ValueError("invalid limit")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT match_id::text, pbp_s3_path FROM public.matches "
                "WHERE duplicate_of IS NULL AND pbp_s3_path IS NOT NULL "
                "AND pbp_s3_path LIKE 'pbp/%%' "
                "ORDER BY match_date DESC NULLS LAST, match_id LIMIT %s", (limit * 4,)
            )
            rows = cursor.fetchall()
    except Exception as exc:
        raise CandidateReadError("sql") from exc
    result = []
    try:
        for match_id, path in rows:
            keys = related_partition_keys(path)
            if keys is not None:
                result.append((str(match_id), keys))
            if len(result) >= limit:
                break
    except Exception as exc:
        raise CandidateReadError("processing") from exc
    return result
