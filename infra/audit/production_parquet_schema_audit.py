"""Standalone bounded Parquet metadata audit. No application imports or row reads."""

import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

PREFIXES = ("pbp/", "nbbet_stats/", "nbbet_details/")
MAX_FOOTER = 1024 * 1024
MAX_BYTES = 12 * 1024 * 1024
MAX_OUTPUT = 32768
MAX_CALLS = 60  # 6 lists + 18 * (HEAD + 2 GET)
MAX_ATTEMPTS = 156
FIELDS = ("S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
ERRORS = {
    "not_parquet",
    "invalid_footer",
    "unsupported_schema",
    "access_denied",
    "transport_error",
    "service_error",
    "budget_exhausted",
}
# Only application writer field names may leave the host. Arbitrary ASCII is not safe.
SAFE_NAMES = {
    "canonical_match_id",
    "nbbet_match_id",
    "gender",
    "level",
    "surface",
    "set_num",
    "game_num",
    "point_num",
    "server",
    "point_winner",
    "server_won_point",
    "score_server",
    "score_returner",
    "is_break_point",
    "is_match_point",
    "is_tiebreak",
    "point_type",
    "game_complete",
    "status",
    "has_coverage",
    "sets_json",
    "extra_json",
    "players_json",
    "p1_rank",
    "p2_rank",
    "p1_rank_tour",
    "p2_rank_tour",
    "details_version",
    "scope",
}
SAFE_NAMES.update(
    f"{name}_p{p}"
    for name in (
        "aces",
        "double_faults",
        "first_serve_points_all",
        "first_serve_points_won",
        "second_serve_points_all",
        "second_serve_points_won",
        "break_points_won",
        "break_points_all",
        "winners",
        "unforced_errors",
        "max_points_in_row",
        "max_games_in_row",
        "service_games_won",
        "service_games_all",
    )
    for p in (1, 2)
)


class Failure(Exception):
    pass


def classify(exc):
    if (
        isinstance(exc, Failure)
        and exc.args
        and isinstance(exc.args[0], str)
        and exc.args[0] in ERRORS
    ):
        return exc.args[0]
    response = getattr(exc, "response", {})
    if (
        isinstance(response, dict)
        and response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403
    ):
        return "access_denied"
    if isinstance(exc, TimeoutError) or type(exc).__name__ in (
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "EndpointConnectionError",
    ):
        return "transport_error"
    return "service_error"


def parse_env(raw):
    if len(raw) > 65536:
        raise Failure("service_error")
    result = {}
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if not match:
            if any(re.match(r"(?:export\s+)?" + k + r"\b", line) for k in FIELDS):
                raise Failure("service_error")
            continue
        key, value = match.groups()
        if key not in FIELDS:
            continue
        if key in result:
            raise Failure("service_error")
        value = value.strip()
        if value.startswith(('"', "'")):
            q = value[0]
            end = value.find(q, 1)
            if end < 0 or (
                value[end + 1 :].strip() and not value[end + 1 :].lstrip().startswith("#")
            ):
                raise Failure("service_error")
            value = value[1:end]
        elif not re.fullmatch(r"[^\s#'\"\\]+", value):
            raise Failure("service_error")
        if not value or len(value) > 4096 or any(c in value for c in ("\\", "$", "`", "\x00")):
            raise Failure("service_error")
        result[key] = value
    return result


def configuration():
    with Path("/root/Tennis_pelmen_model/.env").open("rb") as stream:
        values = parse_env(stream.read(65537))
    # Do not silently overlay or interpolate configuration.
    if any(not values.get(k) for k in FIELDS):
        raise Failure("service_error")
    if (
        values["S3_ENDPOINT_URL"] != "https://storage.yandexcloud.net"
        or values["S3_REGION"] != "ru-central1"
    ):
        raise Failure("service_error")
    bucket = values["S3_BUCKET"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or ".." in bucket:
        raise Failure("service_error")
    try:
        ipaddress.ip_address(bucket)
    except ValueError:
        return values
    raise Failure("service_error")


class Reader:
    def __init__(self, client, bucket, clock=time.monotonic):
        self.client, self.bucket, self.clock = client, bucket, clock
        self.deadline = clock() + 75
        self.calls = self.attempts = self.bytes = 0

    def check(self):
        if self.clock() >= self.deadline:
            raise Failure("budget_exhausted")

    def operation_guard(self, model, params, **kwargs):
        if model.name not in {"ListObjectsV2", "HeadObject", "GetObject"}:
            raise Failure("service_error")
        if params.get("Bucket") != self.bucket:
            raise Failure("service_error")
        if model.name == "ListObjectsV2":
            if params.get("Prefix") not in PREFIXES or params.get("MaxKeys") != 500:
                raise Failure("service_error")
        else:
            key = params.get("Key")
            if not isinstance(key, str) or not any(key.startswith(p) for p in PREFIXES):
                raise Failure("service_error")
        if model.name == "GetObject":
            match = re.fullmatch(r"bytes=([0-9]+)-([0-9]+)", params.get("Range", ""))
            if not match or not params.get("IfMatch"):
                raise Failure("service_error")
            start, end = map(int, match.groups())
            if not 0 < end - start + 1 <= MAX_FOOTER:
                raise Failure("service_error")

    def after_call(self, model, http_response, **kwargs):
        # GET streams are counted by ranged(); LIST bodies have already been
        # parsed by botocore. Include their actual response bytes in the budget.
        if model.name == "ListObjectsV2":
            self.bytes += len(http_response.content)
            if self.bytes > MAX_BYTES:
                raise Failure("budget_exhausted")
        if model.name != "GetObject":
            self.check()

    def before_send(self, request=None, **kwargs):
        self.check()
        if request is not None:
            target = urlsplit(request.url)
            if (
                target.scheme != "https"
                or target.hostname != "storage.yandexcloud.net"
                or target.port not in (None, 443)
                or target.username
                or target.password
            ):
                raise Failure("service_error")
        if self.attempts >= MAX_ATTEMPTS:
            raise Failure("budget_exhausted")
        self.attempts += 1

    def call(self, operation, **kwargs):
        self.check()
        if operation not in {"list_objects_v2", "head_object", "get_object"}:
            raise Failure("service_error")
        names = {
            "list_objects_v2": "ListObjectsV2",
            "head_object": "HeadObject",
            "get_object": "GetObject",
        }
        self.operation_guard(
            SimpleNamespace(name=names[operation]), {"Bucket": self.bucket, **kwargs}
        )
        if self.calls >= MAX_CALLS:
            raise Failure("budget_exhausted")
        self.calls += 1
        result = getattr(self.client, operation)(Bucket=self.bucket, **kwargs)
        try:
            self.check()
        except Failure:
            if operation == "get_object" and result.get("Body") is not None:
                result["Body"].close()
            raise
        return result

    def ranged(self, key, start, end, size, etag):
        length = end - start + 1
        if start < 0 or length <= 0 or self.bytes + length > MAX_BYTES:
            raise Failure("budget_exhausted")
        # Reserve before I/O: partial reads/timeouts cannot escape accounting.
        self.bytes += length
        result = self.call("get_object", Key=key, Range=f"bytes={start}-{end}", IfMatch=etag)
        body = result.get("Body")
        try:
            if (
                result.get("ResponseMetadata", {}).get("HTTPStatusCode") != 206
                or result.get("ContentRange") != f"bytes {start}-{end}/{size}"
                or result.get("ContentLength") != length
                or result.get("ETag") != etag
            ):
                raise Failure("invalid_footer")
            # Read exactly the verified range; never drain an ignored range response.
            data = body.read(length)
            self.check()
            if len(data) != length:
                raise Failure("invalid_footer")
            return data
        finally:
            if body is not None:
                body.close()

    def metadata(self, key):
        import pyarrow as pa
        import pyarrow.parquet as pq

        head = self.call("head_object", Key=key)
        size, etag = head.get("ContentLength"), head.get("ETag")
        if type(size) is not int or size < 12 or not isinstance(etag, str) or not etag:
            raise Failure("invalid_footer")
        tail = self.ranged(key, size - 8, size - 1, size, etag)
        if tail[4:] != b"PAR1":
            raise Failure("not_parquet")
        length = struct.unpack("<I", tail[:4])[0]
        if not 0 < length <= MAX_FOOTER or length > size - 12:
            raise Failure("invalid_footer")
        footer = self.ranged(key, size - 8 - length, size - 9, size, etag)
        try:
            # Synthetic envelope: Arrow sees only metadata, never original row bytes.
            meta = pq.ParquetFile(
                pa.BufferReader(b"PAR1" + footer + tail),
                thrift_string_size_limit=MAX_FOOTER,
                thrift_container_size_limit=10000,
            ).metadata
            schema = meta.schema.to_arrow_schema()
            if len(schema) > 128 or meta.num_columns > 128 or meta.num_row_groups > 10000:
                raise Failure("unsupported_schema")
            if not 0 <= meta.num_rows <= 2**63 - 1:
                raise Failure("invalid_footer")
            columns = []
            redacted = 0
            for index in range(meta.num_columns):
                column = meta.schema.column(index)
                parts = column.path.split(".")
                safe = len(parts) <= 8 and all(
                    p in SAFE_NAMES or p in {"list", "element", "item"} for p in parts
                )
                if not safe:
                    redacted += 1
                    continue
                # Logical types are reduced to fixed categories: no custom strings/metadata.
                logical = str(column.logical_type).split("(", 1)[0]
                if logical not in {
                    "None",
                    "String",
                    "Int",
                    "Decimal",
                    "Date",
                    "Time",
                    "Timestamp",
                    "List",
                    "Map",
                    "JSON",
                    "BSON",
                    "UUID",
                    "Null",
                    "Float16",
                }:
                    logical = "other"
                physical = column.physical_type
                if physical not in {
                    "BOOLEAN",
                    "INT32",
                    "INT64",
                    "INT96",
                    "FLOAT",
                    "DOUBLE",
                    "BYTE_ARRAY",
                    "FIXED_LEN_BYTE_ARRAY",
                }:
                    physical = "other"
                columns.append(
                    {
                        "path": column.path,
                        "physical": physical,
                        "logical": logical,
                        "definition_level": column.max_definition_level,
                        "repetition_level": column.max_repetition_level,
                    }
                )
            self.check()
            return {
                "size_bucket": "<1MiB" if size < MAX_FOOTER else ">=1MiB",
                "rows": meta.num_rows,
                "row_groups": meta.num_row_groups,
                "fingerprint": hashlib.sha256(
                    schema.remove_metadata().serialize().to_pybytes()
                ).hexdigest(),
                "columns": columns,
                "redacted_columns": redacted,
            }
        except Failure:
            raise
        except Exception:  # noqa: BLE001 — never expose parser/config exception text
            raise Failure("invalid_footer") from None


def audit(reader):
    report = {"version": 1, "semantic_verification": "pending", "prefixes": []}
    for prefix in PREFIXES:
        item = {
            "prefix": prefix,
            "complete": False,
            "observed_counts_are_lower_bounds": True,
            "observed_keys": 0,
            "extensions": {"parquet": 0, "other": 0, "directory": 0},
            "samples": [],
            "sampled_objects": 0,
            "errors": {},
            "fingerprints": {},
            "sampled_rows": 0,
        }
        report["prefixes"].append(item)
        keys, seen, token = [], set(), None
        listing_complete = False
        try:
            for _ in range(2):
                args = {"Prefix": prefix, "MaxKeys": 500}
                if token is not None:
                    args["ContinuationToken"] = token
                page = reader.call("list_objects_v2", **args)
                contents = page.get("Contents", [])
                if not isinstance(contents, list) or len(contents) > 500:
                    raise Failure("service_error")
                for obj in contents:
                    key = obj.get("Key")
                    if (
                        not isinstance(key, str)
                        or not key.startswith(prefix)
                        or len(key.encode()) > 1024
                    ):
                        raise Failure("service_error")
                    if key in seen:
                        raise Failure("service_error")
                    seen.add(key)
                    item["observed_keys"] += 1
                    kind = (
                        "directory"
                        if key.endswith("/")
                        else ("parquet" if key.endswith(".parquet") else "other")
                    )
                    item["extensions"][kind] += 1
                    if kind == "parquet":
                        keys.append(key)
                truncated = page.get("IsTruncated")
                if type(truncated) is not bool:
                    raise Failure("service_error")
                if not truncated:
                    listing_complete = True
                    break
                next_token = page.get("NextContinuationToken")
                if (
                    not isinstance(next_token, str)
                    or not next_token
                    or len(next_token) > 4096
                    or next_token == token
                ):
                    raise Failure("service_error")
                token = next_token
            keys.sort()
            n = min(6, len(keys))
            selected = [keys[i * (len(keys) - 1) // max(1, n - 1)] for i in range(n)]
            for key in selected:
                item["sampled_objects"] += 1
                try:
                    sample = reader.metadata(key)
                    item["samples"].append(sample)
                    fp = sample["fingerprint"]
                    item["fingerprints"][fp] = item["fingerprints"].get(fp, 0) + 1
                    item["sampled_rows"] += sample["rows"]
                except Exception as exc:  # noqa: BLE001 — classify without printing raw errors
                    reason = classify(exc)
                    item["errors"][reason] = item["errors"].get(reason, 0) + 1
            item["complete"] = listing_complete and not item["errors"]
            item["observed_counts_are_lower_bounds"] = not item["complete"]
        except Exception as exc:  # noqa: BLE001 — classify without printing raw errors
            reason = classify(exc)
            item["errors"][reason] = item["errors"].get(reason, 0) + 1
    report["budget"] = {
        "sdk_calls": reader.calls,
        "http_attempts": reader.attempts,
        "response_body_bytes_accounted": reader.bytes,
    }
    report["sampling"] = "lexicographically_spaced_observed_keys_not_representative"
    return report


def encode(report):
    raw = json.dumps(report, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > MAX_OUTPUT:
        # Fail closed, without accidentally marking omitted prefixes complete.
        raw = json.dumps(
            {
                "version": 1,
                "complete": False,
                "error": "budget_exhausted",
                "reason": "output_limit",
                "semantic_verification": "pending",
                "prefixes": [
                    {"prefix": p, "complete": False, "observed_counts_are_lower_bounds": True}
                    for p in PREFIXES
                ],
            }
        )
    return raw


def main():
    logging.disable(logging.CRITICAL)
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.alarm(110)
    try:
        import boto3
        from botocore.config import Config

        values = configuration()
        client = boto3.client(
            "s3",
            endpoint_url=values["S3_ENDPOINT_URL"],
            region_name=values["S3_REGION"],
            aws_access_key_id=values["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=values["S3_SECRET_ACCESS_KEY"],
            config=Config(
                connect_timeout=2,
                read_timeout=3,
                s3={"addressing_style": "path"},
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )
        reader = Reader(client, values["S3_BUCKET"])
        client.meta.events.register("before-parameter-build.s3", reader.operation_guard)
        client.meta.events.register("after-call.s3", reader.after_call)
        client.meta.events.register("before-send.s3", reader.before_send)
        report = audit(reader)
        encoded = encode(report)
        print(encoded)
        return 0 if all(p["complete"] for p in json.loads(encoded)["prefixes"]) else 1
    except Exception:  # noqa: BLE001 — never expose parser/config exception text
        print('{"complete":false,"error":"audit_unavailable"}')
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
