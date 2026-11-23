"""Offline fixtures only: no credentials, production connections or S3 requests."""

import importlib.util
import io
import json
import struct
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SPEC = importlib.util.spec_from_file_location(
    "parquet_audit", Path(__file__).parents[1] / "infra/audit/production_parquet_schema_audit.py"
)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def parquet(name="point_winner", values=None):
    stream = io.BytesIO()
    pq.write_table(pa.table({name: values or [1, 2, 1]}), stream, row_group_size=2)
    return stream.getvalue()


class Body(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.bytes_read = 0

    def read(self, n=-1):
        assert n >= 0, "Unbounded read forbidden"
        data = super().read(n)
        self.bytes_read += len(data)
        return data


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.calls = []
        self.bodies = []
        self.ignored_range = False
        self.bad_range = False
        self.short = False
        self.timeout = False

    def list_objects_v2(self, **kw):
        self.calls.append(("list", kw))
        if self.timeout:
            raise TimeoutError("secret token player match-id object-key")
        keys = sorted(k for k in self.objects if k.startswith(kw["Prefix"]))
        offset = int(kw.get("ContinuationToken", "0"))
        selected = keys[offset : offset + kw["MaxKeys"]]
        end = offset + len(selected)
        result = {"Contents": [{"Key": k} for k in selected], "IsTruncated": end < len(keys)}
        if result["IsTruncated"]:
            result["NextContinuationToken"] = str(end)
        return result

    def head_object(self, **kw):
        self.calls.append(("head", kw))
        return {"ContentLength": len(self.objects[kw["Key"]]), "ETag": "etag"}

    def get_object(self, **kw):
        self.calls.append(("get", kw))
        data = self.objects[kw["Key"]]
        start, end = map(int, kw["Range"].removeprefix("bytes=").split("-"))
        body = Body(data if self.ignored_range else data[start : end + 1])
        if self.short:
            body = Body(data[start:end])
        self.bodies.append(body)
        return {
            "Body": body,
            "ContentLength": end - start + 1,
            "ETag": "etag",
            "ContentRange": "invalid" if self.bad_range else f"bytes {start}-{end}/{len(data)}",
            "ResponseMetadata": {"HTTPStatusCode": 200 if self.ignored_range else 206},
        }


def test_valid_metadata_only():
    data = parquet()
    client = FakeS3({"pbp/secret-match.parquet": data})
    reader = audit.Reader(client, "private-bucket")
    result = audit.audit(reader)
    sample = result["prefixes"][0]["samples"][0]
    assert sample["rows"] == 3 and sample["row_groups"] == 2
    assert sample["columns"][0]["path"] == "point_winner"
    gets = [kw for op, kw in client.calls if op == "get"]
    assert len(gets) == 2
    footer_len = struct.unpack("<I", data[-8:-4])[0]
    assert gets[0]["Range"] == f"bytes={len(data) - 8}-{len(data) - 1}"
    assert gets[1]["Range"] == f"bytes={len(data) - 8 - footer_len}-{len(data) - 9}"
    assert reader.bytes == footer_len + 8
    assert all(b.closed for b in client.bodies)
    output = audit.encode(result)
    assert "secret-match" not in output and "private-bucket" not in output


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda b: b[:-4] + b"NOPE", "not_parquet"),
        (lambda b: b[:-8] + struct.pack("<I", audit.MAX_FOOTER + 1) + b"PAR1", "invalid_footer"),
        (lambda b: b[:-8] + struct.pack("<I", 0) + b"PAR1", "invalid_footer"),
        (lambda b: b"PAR1" + b"x" * 16 + struct.pack("<I", 16) + b"PAR1", "invalid_footer"),
        (lambda b: b"PAR1" + struct.pack("<I", 100) + b"PAR1", "invalid_footer"),
    ],
)
def test_malformed_footer(mutation, reason):
    client = FakeS3({"pbp/a.parquet": mutation(parquet())})
    result = audit.audit(audit.Reader(client, "bucket"))
    assert result["prefixes"][0]["errors"] == {reason: 1}
    assert not result["prefixes"][0]["complete"]


@pytest.mark.parametrize("flag", ["ignored_range", "bad_range", "short"])
def test_invalid_range(flag):
    client = FakeS3({"pbp/a.parquet": parquet()})
    setattr(client, flag, True)
    result = audit.audit(audit.Reader(client, "bucket"))
    assert result["prefixes"][0]["errors"] == {"invalid_footer": 1}
    assert all(b.closed for b in client.bodies)
    if flag == "ignored_range":
        assert all(b.bytes_read == 0 for b in client.bodies)


def test_pagination_and_sample_bounds():
    data = parquet()
    client = FakeS3(
        {f"{prefix}{i:04}.parquet": data for prefix in audit.PREFIXES for i in range(1001)}
    )
    result = audit.audit(audit.Reader(client, "bucket"))
    assert sum(op == "list" for op, _ in client.calls) == 6
    assert sum(op == "head" for op, _ in client.calls) == 18
    assert sum(op == "get" for op, _ in client.calls) == 36
    for prefix in result["prefixes"]:
        assert prefix["observed_keys"] == 1000
        assert prefix["observed_counts_are_lower_bounds"]
        assert not prefix["complete"]
        assert len(prefix["samples"]) == 6
    assert not any("nbbet_raw/" in kw.get("Prefix", "") for _, kw in client.calls)


def test_zero_parquet_and_nonparquet_classification():
    client = FakeS3({"pbp/a.json": b"private", "pbp/dir/": b"", "pbp/a.PARQUET": b"x"})
    result = audit.audit(audit.Reader(client, "bucket"))
    assert result["prefixes"][0]["extensions"] == {"parquet": 0, "other": 2, "directory": 1}
    assert result["prefixes"][0]["complete"]
    assert all(op == "list" for op, _ in client.calls)


def test_timeout_redaction():
    client = FakeS3()
    client.timeout = True
    result = audit.audit(audit.Reader(client, "bucket"))
    assert all(p["errors"] == {"transport_error": 1} for p in result["prefixes"])
    assert "secret" not in audit.encode(result)


def test_deadline_and_operation_guard():
    now = [0]
    reader = audit.Reader(FakeS3(), "bucket", clock=lambda: now[0])
    with pytest.raises(audit.Failure):
        reader.call("put_object", Key="a", Body=b"x")
    assert reader.calls == 0
    now[0] = 75
    with pytest.raises(audit.Failure, match="budget_exhausted"):
        reader.call("list_objects_v2", Prefix="pbp/")


def test_byte_budget_stops_before_get():
    client = FakeS3({"pbp/a.parquet": parquet()})
    reader = audit.Reader(client, "bucket")
    reader.bytes = audit.MAX_BYTES
    with pytest.raises(audit.Failure, match="budget_exhausted"):
        reader.metadata("pbp/a.parquet")
    assert not any(op == "get" for op, _ in client.calls)


def test_http_attempt_budget():
    reader = audit.Reader(FakeS3(), "bucket")
    for _ in range(audit.MAX_ATTEMPTS):
        reader.before_send()
    with pytest.raises(audit.Failure, match="budget_exhausted"):
        reader.before_send()


def test_schema_redaction_and_nested_metadata():
    client = FakeS3(
        {
            "pbp/a.parquet": parquet("player_Alice_token_123"),
            "nbbet_stats/a.parquet": parquet("point_winner", [[1, 2], [2]]),
        }
    )
    output = audit.encode(audit.audit(audit.Reader(client, "bucket")))
    assert "Alice" not in output
    report = json.loads(output)
    assert report["prefixes"][0]["samples"][0]["redacted_columns"] == 1
    assert report["prefixes"][1]["samples"][0]["columns"][0]["repetition_level"] == 1


def test_output_compacts_repeated_schema_without_losing_counts():
    sample = {
        "fingerprint": "a" * 64,
        "rows": 3,
        "columns": [{"path": "point_winner", "physical": "INT64", "logical": "None",
                     "definition_level": 1, "repetition_level": 0}] * 100,
    }
    report = {
        "version": 1,
        "semantic_verification": "pending",
        "prefixes": [
            {"prefix": prefix, "complete": True, "observed_keys": 1000,
             "sampled_objects": 6, "sampled_rows": 18,
             "fingerprints": {"a" * 64: 6}, "errors": {},
             "samples": [dict(sample, columns=list(sample["columns"])) for _ in range(6)]}
            for prefix in audit.PREFIXES
        ],
    }
    result = json.loads(audit.encode(report))
    assert len(audit.encode(report).encode()) <= audit.MAX_OUTPUT
    assert all(p["complete"] and p["observed_keys"] == 1000 for p in result["prefixes"])
    assert all(p["fingerprints"]["a" * 64] == 6 for p in result["prefixes"])
    assert all(len(p["samples"]) == 6 for p in result["prefixes"])
    assert all(p["samples"][1].get("columns_omitted") == "duplicate_schema"
               for p in result["prefixes"])


def test_output_retains_aggregates_if_all_sample_columns_too_large():
    report = {
        "version": 1,
        "semantic_verification": "pending",
        "prefixes": [
            {"prefix": "pbp/", "complete": True, "observed_keys": 1000,
             "fingerprints": {"f" * 64: 1},
             "samples": [{"fingerprint": "f" * 64, "rows": 1,
                          "columns": [{"path": "point_winner", "physical": "INT64",
                                       "logical": "None"}] * 2500}]}
        ],
    }
    result = json.loads(audit.encode(report))
    assert len(audit.encode(report).encode()) <= audit.MAX_OUTPUT
    assert result["prefixes"][0]["observed_keys"] == 1000
    assert result["prefixes"][0]["complete"]
    assert result["prefixes"][0]["samples"][0]["columns_omitted"] == "output_limit"


def test_output_cap():
    output = audit.encode({"prefixes": [{"complete": True, "data": "x" * 40000}]})
    assert len(output.encode()) <= audit.MAX_OUTPUT
    assert json.loads(output)["complete"] is False


@pytest.mark.parametrize(
    "raw",
    [
        b"S3_BUCKET=a\nS3_BUCKET=b",
        b"S3_BUCKET=${SECRET}",
        b'S3_BUCKET="unterminated',
        b"x" * 65537,
        b"S3_BUCKET=$(command)",
        b"S3_BUCKET `command`",
    ],
)
def test_literal_config_rejections(raw):
    with pytest.raises(audit.Failure):
        audit.parse_env(raw)


def test_literal_config():
    assert audit.parse_env(b'# comment\nexport S3_BUCKET="safe-bucket" # comment\nOTHER=x') == {
        "S3_BUCKET": "safe-bucket"
    }


def test_sdk_parameter_guard_and_redirect_rejection():
    from types import SimpleNamespace

    reader = audit.Reader(FakeS3(), "bucket")
    for name, params in [
        ("PutObject", {"Bucket": "bucket"}),
        ("GetObject", {"Bucket": "bucket", "Key": "pbp/a.parquet"}),
        (
            "GetObject",
            {"Bucket": "bucket", "Key": "nbbet_raw/a", "Range": "bytes=0-7", "IfMatch": "x"},
        ),
        ("ListObjectsV2", {"Bucket": "bucket", "Prefix": "nbbet_raw/", "MaxKeys": 500}),
        ("HeadObject", {"Bucket": "other", "Key": "pbp/a"}),
    ]:
        with pytest.raises(audit.Failure):
            reader.operation_guard(SimpleNamespace(name=name), params)
    reader.operation_guard(
        SimpleNamespace(name="GetObject"),
        {"Bucket": "bucket", "Key": "pbp/a.parquet", "Range": "bytes=10-17", "IfMatch": "x"},
    )
    with pytest.raises(audit.Failure):
        reader.before_send(request=SimpleNamespace(url="https://attacker.invalid/key"))


def test_list_response_body_budget():
    from types import SimpleNamespace

    reader = audit.Reader(FakeS3(), "bucket")
    reader.after_call(SimpleNamespace(name="ListObjectsV2"), SimpleNamespace(content=b"xml"))
    assert reader.bytes == 3
    reader.bytes = audit.MAX_BYTES
    with pytest.raises(audit.Failure, match="budget_exhausted"):
        reader.after_call(SimpleNamespace(name="ListObjectsV2"), SimpleNamespace(content=b"x"))


def test_changed_object_and_service_error_classification():
    class AccessDenied(Exception):
        def __init__(self, text):
            super().__init__(text)
            self.response = {"ResponseMetadata": {"HTTPStatusCode": 403}}

    assert audit.classify(AccessDenied("private")) == "access_denied"
    assert audit.classify(ValueError("private")) == "service_error"
    client = FakeS3({"pbp/a.parquet": parquet()})
    original = client.get_object

    def changed(**kw):
        result = original(**kw)
        result["ETag"] = "changed"
        return result

    client.get_object = changed
    result = audit.audit(audit.Reader(client, "bucket"))
    assert result["prefixes"][0]["errors"] == {"invalid_footer": 1}
    assert all(b.bytes_read == 0 and b.closed for b in client.bodies)


def test_request_limit_and_invalid_pagination():
    reader = audit.Reader(FakeS3(), "bucket")
    reader.calls = audit.MAX_CALLS
    with pytest.raises(audit.Failure, match="budget_exhausted"):
        reader.call("head_object", Key="pbp/a")

    class Broken(FakeS3):
        def list_objects_v2(self, **kw):
            return {"Contents": [], "IsTruncated": True}

    report = audit.audit(audit.Reader(Broken(), "bucket"))
    assert all(not p["complete"] for p in report["prefixes"])


def test_no_row_read_apis_or_application_imports():
    import ast

    tree = ast.parse(Path(SPEC.origin).read_text())
    forbidden = {
        "read_table",
        "read_row_group",
        "read_row_groups",
        "iter_batches",
        "put_object",
        "upload_file",
        "delete_object",
        "download_file",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("tennis_predictor")


def test_stream_closed_when_deadline_expires_during_get():
    now = [0]
    client = FakeS3({"pbp/a.parquet": parquet()})
    original = client.get_object

    def delayed(**kw):
        result = original(**kw)
        now[0] = 76
        return result

    client.get_object = delayed
    reader = audit.Reader(client, "bucket", clock=lambda: now[0])
    with pytest.raises(audit.Failure, match="budget_exhausted"):
        reader.metadata("pbp/a.parquet")
    assert all(b.closed and b.bytes_read == 0 for b in client.bodies)
