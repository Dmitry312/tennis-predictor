import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("identity_s3", ROOT / "infra/audit/player_identity_audit.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def fixture(field="players_json", rows=1):
    b = io.BytesIO()
    player = {"1": "Private Alice", "2": "https://private.invalid/photo", "4": {"1": 31}, "7": 987654321}
    obj = {"p1": player, "p2": player} if field == "players_json" else {
        "match": {"3": player, "5": player}, "private-map": {"654321987": player}}
    pq.write_table(pa.table({field: [json.dumps(obj)] * rows}), b)
    return b.getvalue()


class Body(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.reads = []
    def read(self, n=-1):
        assert 0 <= n <= a.MAX_FILE
        self.reads.append(n)
        return super().read(n)


class Fake:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.calls, self.bodies = [], []
        self.bad_length = self.truncated = False
    def list_objects_v2(self, **kw):
        self.calls.append(("list", kw))
        prefix = kw["Prefix"]
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        if "Delimiter" in kw:
            prefixes = sorted({prefix + k[len(prefix):].split("/")[0] + "/" for k in keys})
            return {"CommonPrefixes": [{"Prefix": p} for p in prefixes[:128]],
                    "IsTruncated": self.truncated or len(prefixes) > 128}
        return {"Contents": [{"Key": k, "Size": len(self.objects[k]), "ETag": "tag"} for k in keys[:128]],
                "IsTruncated": self.truncated or len(keys) > 128}
    def get_object(self, **kw):
        self.calls.append(("get", kw))
        data = self.objects[kw["Key"]]
        b = Body(data)
        self.bodies.append(b)
        return {"Body": b, "ContentLength": len(data) + int(self.bad_length), "ETag": "tag",
                "ResponseMetadata": {"HTTPStatusCode": 200}}


def test_stratification_redaction_repetition():
    objects = {root + "gender=" + g + "/level=" + l + "/year=" + str(y) + "/private-key.parquet":
               fixture("players_json" if root == "nbbet_details/" else "raw_json")
               for root in a.ROOTS for g in ("M", "F") for l in ("tour", "challenger")
               for y in (2018, 2020, 2024)}
    f = Fake(objects)
    r = a.Reader(f, "secret-bucket")
    report = a.collect(r)
    output = a.encode(report)
    assert r.lists == 32 and r.gets == 24
    assert report["bounded_scan_finished"] and not report["complete"]
    assert all(b.closed and len(b.reads) == 1 for b in f.bodies)
    for secret in ("Private Alice", "987654321", "654321987", "private-key", "private-map", "secret-bucket", "https://"):
        assert secret not in output
    p = next(p for p in report["paths"] if p["scope"] == "details.player" and p["path"] == "field_7")
    assert p["cross_year_values"] == p["cross_gender_values"] == 1
    assert any(p["scope"].endswith("mapping_key") and p["path"] == "field_64" for p in report["paths"])


def test_consistency():
    e = a.Evidence()
    for year, name, ident in [(2020, "Alias One", 99999991), (2021, "Alias Two", 99999991), (2022, "Alias Two", 99999992)]:
        e.observe("details", year, "M", {"players_json": json.dumps({"p1": {"1": name, "7": ident}})})
    p = next(p for p in e.report()["paths"] if p["path"] == "field_7")
    assert p["values_with_multiple_names"] == p["names_with_multiple_values"] == p["cross_year_values"] == 1


def test_empty_truncation():
    f = Fake()
    f.truncated = True
    report = a.collect(a.Reader(f, "bucket"))
    assert report["decoded_rows"] == 0
    assert all(s["year_listing_truncated"] for s in report["strata"])


@pytest.mark.parametrize("raw", ["bad", "[]", "null", None, "x"*65537])
def test_invalid_json(raw):
    e = a.Evidence()
    e.observe("details", 2020, "M", {"players_json": raw})
    assert e.report()["invalid_json_rows"] == 1


def test_non_id_and_deduplication():
    e = a.Evidence()
    row = {"players_json": json.dumps({"p1": {"1": "Name", "2": "photo", "4": {"1": 30}}})}
    e.observe("details", 2020, "M", row)
    e.observe("details", 2020, "M", row)
    assert e.report()["unique_source_year_rows"] == 1
    assert all(p["classification"] == "known_name_photo_flag_or_rank" for p in e.report()["paths"])


@pytest.mark.parametrize("data", [b"bad", b"PAR1brokenPAR1", fixture(rows=257)])
def test_bad_parquet(data):
    with pytest.raises(Exception):
        a.decode(data, "players_json")


def test_decode():
    assert len(a.decode(fixture(), "players_json")) == 1
    with pytest.raises(a.Stop):
        a.decode(fixture("raw_json"), "players_json")


def test_stream_closure():
    key = "nbbet_details/gender=M/level=tour/year=2020/a.parquet"
    f = Fake({key: fixture()})
    f.bad_length = True
    with pytest.raises(a.Stop):
        a.Reader(f, "bucket").get({"Key": key, "ETag": "tag", "Size": len(f.objects[key])})
    assert f.bodies[0].closed and not f.bodies[0].reads


def test_budgets():
    f = Fake()
    r = a.Reader(f, "bucket")
    with pytest.raises(a.Stop):
        r.get({"Key": "x", "Size": a.MAX_FILE+1, "ETag": "tag"})
    assert not f.calls
    now = [0]
    r = a.Reader(f, "bucket", clock=lambda: now[0])
    now[0] = 65
    with pytest.raises(a.Stop):
        r.page("nbbet_details/gender=M/level=tour/")
    r = a.Reader(f, "bucket")
    r.lists = a.MAX_LISTS
    with pytest.raises(a.Stop):
        r.page("nbbet_details/gender=M/level=tour/")
    r.attempts = a.MAX_LISTS+a.MAX_GETS
    with pytest.raises(a.Stop):
        r.before_send(SimpleNamespace(url="https://storage.yandexcloud.net/bucket"))
    r.bytes = a.MAX_BYTES
    with pytest.raises(a.Stop):
        r.get({"Size": 100, "ETag": "tag"})


@pytest.mark.parametrize("op,params", [
    ("put_object", {}), ("delete_object", {}),
    ("get_object", {"Key": "nbbet_raw/gender=M/level=itf/year=2020/a.parquet", "IfMatch": "tag"}),
    ("get_object", {"Key": "nbbet_raw/gender=M/level=tour/year=2020/a.parquet"}),
    ("list_objects_v2", {"Prefix": "nbbet_raw/", "MaxKeys": 128})])
def test_allowlist(op, params):
    f = Fake()
    with pytest.raises(a.Stop):
        a.Reader(f, "bucket").call(op, **params)
    assert not f.calls


def test_limits():
    e = a.Evidence()
    with pytest.raises(a.Stop):
        e.observe("details", 2020, "M", {"players_json": json.dumps({"p1": {"7": list(range(129))}})})
    with pytest.raises(a.Stop):
        a.encode({"x": "x"*a.MAX_OUTPUT})
    e.rows = a.MAX_ROWS
    with pytest.raises(a.Stop):
        e.observe("details", 2020, "M", {})
    with pytest.raises(a.Stop):
        a.Reader(Fake(), "bucket").before_send(SimpleNamespace(url="https://evil.invalid/secret"))


@pytest.mark.parametrize("raw", [b"S3_BUCKET=a\nS3_BUCKET=b", b"S3_BUCKET=$value", b"S3_BUCKET='broken", b"x"*65537])
def test_config_rejection(raw):
    with pytest.raises(a.Stop):
        a.config(raw)


def test_workflow_safety():
    path = ROOT / ".github/workflows/production-player-identity-audit.yml"
    w = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(w["on"]) == {"workflow_dispatch"}
    job = next(iter(w["jobs"].values()))
    assert "refs/heads/ML" in job["if"]
    assert job["environment"] == "player-identity-audit"
    for required in ("REVIEWED_SHA", "SELECTED_SHA", "StrictHostKeyChecking=yes", "ed25519",
                     "ForwardAgent=no", "-I -B -", "player_identity_audit.py"):
        assert required in path.read_text()
    for step in job["steps"]:
        if "uses" in step:
            assert len(step["uses"].split("@")[1]) == 40
        if "run" in step:
            subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)


def test_no_http_retry_and_prelist_byte_reservation():
    r = a.Reader(Fake(), "bucket")
    request = SimpleNamespace(url="https://storage.yandexcloud.net/bucket")
    r.before_send(request)
    with pytest.raises(a.Stop):
        r.before_send(request)
    f = Fake()
    r = a.Reader(f, "bucket")
    r.bytes = a.MAX_BYTES - 100
    with pytest.raises(a.Stop):
        r.page("nbbet_details/gender=M/level=tour/")
    assert not f.calls


def test_read_timeout_closes_stream():
    class TimeoutBody(Body):
        def read(self, n=-1):
            raise TimeoutError("synthetic private value")
    class TimeoutFake(Fake):
        def get_object(self, **kw):
            result = super().get_object(**kw)
            result["Body"] = TimeoutBody(b"")
            self.bodies.append(result["Body"])
            return result
    key = "nbbet_details/gender=M/level=tour/year=2020/a.parquet"
    f = TimeoutFake({key: fixture()})
    with pytest.raises(a.Diagnostic) as caught:
        a.Reader(f, "bucket").get({"Key": key, "Size": len(f.objects[key]), "ETag": "tag"})
    assert caught.value.stage == "get" and caught.value.reason == "timeout"
    assert f.bodies[-1].closed


def test_strict_literal_configuration():
    raw = b"\n".join([
        b"S3_ENDPOINT_URL=https://storage.yandexcloud.net", b"S3_REGION=ru-central1",
        b"S3_BUCKET=private-bucket", b"S3_ACCESS_KEY_ID='private-access'",
        b"S3_SECRET_ACCESS_KEY='private-secret'"])
    assert a.config(raw)["S3_BUCKET"] == "private-bucket"
    with pytest.raises(a.Stop):
        a.config(raw.replace(b"storage.yandexcloud.net", b"evil.invalid"))


def test_all_scalar_candidate_paths_bounded_and_no_dynamic_keys():
    e = a.Evidence()
    e.observe("details", 2020, "M", {"players_json": json.dumps({
        "p1": {"1": "Secret", "private-source-key": "Private Value", "7": [99999999, 99999998]}})})
    output = a.encode(e.report())
    assert "private-source-key" not in output and "Private Value" not in output
    p = next(p for p in e.report()["paths"] if p["path"] == "field_7.[]")
    assert p["observations"] == 2 and p["objects_with_path"] == 1


@pytest.mark.parametrize("stage", sorted(a.STAGES))
def test_fixed_stage_codes_and_redaction(stage):
    def fail():
        raise ValueError("SECRET-player-token-object-key-path")
    with pytest.raises(a.Diagnostic) as caught:
        a.run_stage(stage, fail)
    report = a.failure_report(caught.value)
    assert report["stage"] == stage
    assert report["reason"] == "internal_error"
    assert not report["complete"] and not report["bounded_scan_finished"]
    assert "SECRET" not in json.dumps(report)


@pytest.mark.parametrize("exc,reason", [
    (ImportError("SECRET"), "dependency_unavailable"),
    (MemoryError("SECRET"), "resource_exhausted"),
    (PermissionError("SECRET"), "access_denied"),
    (FileNotFoundError("SECRET"), "not_found"),
    (TimeoutError("SECRET"), "timeout"),
    (a.Stop("SECRET"), "validation_failed"),
    (a.Stop("budget_exhausted"), "budget_exhausted")])
def test_reason_codes(exc, reason):
    assert a.reason(exc) == reason
    assert "SECRET" not in json.dumps(a.failure_report(exc))


def test_nested_stage_preserved():
    def fail():
        raise a.Stop("unsupported_schema")
    def outer():
        a.run_stage("decode", fail)
    with pytest.raises(a.Diagnostic) as caught:
        a.run_stage("evidence", outer)
    assert a.failure_report(caught.value)["stage"] == "decode"
    assert a.failure_report(a.Diagnostic("SECRET", "SECRET"))["reason"] == "internal_error"


def test_realistic_botocore_responses_and_guards():
    import datetime
    import boto3
    from botocore.config import Config
    from botocore.response import StreamingBody
    from botocore.stub import Stubber
    client = boto3.client("s3", endpoint_url="https://storage.yandexcloud.net",
        region_name="ru-central1", aws_access_key_id="synthetic", aws_secret_access_key="synthetic",
        config=Config(retries={"total_max_attempts": 1}, s3={"addressing_style": "path"}))
    reader = a.Reader(client, "synthetic-bucket")
    client.meta.events.register("before-parameter-build.s3", reader.guard)
    client.meta.events.register("before-send.s3", reader.before_send)
    prefix = "nbbet_details/gender=M/level=tour/year=2020/"
    key, etag, data = prefix+"synthetic.parquet", '"synthetic-etag"', fixture()
    stub = Stubber(client)
    stub.add_response("list_objects_v2", {
        "IsTruncated": False, "Name": "synthetic-bucket", "Prefix": prefix,
        "MaxKeys": 128, "KeyCount": 1,
        "Contents": [{"Key": key, "Size": len(data), "ETag": etag,
                      "LastModified": datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
                      "StorageClass": "STANDARD"}],
        "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {"x-amz-request-id": "SECRET"}}},
        {"Bucket": "synthetic-bucket", "Prefix": prefix, "MaxKeys": 128})
    stream = StreamingBody(io.BytesIO(data), len(data))
    stub.add_response("get_object", {"Body": stream, "ContentLength": len(data), "ETag": etag,
        "ResponseMetadata": {"HTTPStatusCode": 200}},
        {"Bucket": "synthetic-bucket", "Key": key, "IfMatch": etag})
    with stub:
        page = reader.page(prefix)
        assert len(a.decode(reader.get(page["Contents"][0]), "players_json")) == 1
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("status,reason", [(403, "access_denied"), (404, "not_found"), (412, "object_changed"), (500, "service_error")])
def test_sdk_error_redaction(status, reason):
    from botocore.exceptions import ClientError
    exc = ClientError({"Error": {"Code": "SECRET", "Message": "SECRET"},
                       "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObject")
    def fail():
        raise exc
    with pytest.raises(a.Diagnostic) as caught:
        a.run_stage("get", fail)
    assert a.failure_report(caught.value)["reason"] == reason
    assert "SECRET" not in json.dumps(a.failure_report(caught.value))


def test_dotted_bucket_compatibility_and_ip_rejection():
    raw = b"\n".join([b"S3_ENDPOINT_URL=https://storage.yandexcloud.net", b"S3_REGION=ru-central1",
        b"S3_BUCKET=synthetic.bucket", b"S3_ACCESS_KEY_ID=synthetic", b"S3_SECRET_ACCESS_KEY=synthetic"])
    assert a.config(raw)["S3_BUCKET"] == "synthetic.bucket"
    with pytest.raises(a.Diagnostic):
        a.config(raw.replace(b"synthetic.bucket", b"127.0.0.1"))
    with pytest.raises(a.Diagnostic):
        a.config(raw.replace(b"synthetic.bucket", b"synthetic..bucket"))


@pytest.mark.parametrize("failure_stage", sorted(a.STAGES))
def test_main_pipeline_redacts_failure(monkeypatch, capsys, failure_stage):
    import types
    monkeypatch.setattr(a.signal, "signal", lambda *x: None)
    monkeypatch.setattr(a.signal, "alarm", lambda *x: None)
    def failing():
        raise a.Diagnostic(failure_stage, "validation_failed")
    monkeypatch.setattr(a, "resource_limits", failing)
    assert a.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == failure_stage
    assert report["reason"] == "validation_failed"
    assert not report["complete"]


def test_decode_missing_field_and_output_stage():
    with pytest.raises(a.Diagnostic) as caught:
        a.decode(fixture(), "raw_json")
    assert caught.value.stage == "decode" and caught.value.reason == "unsupported_schema"
    with pytest.raises(a.Diagnostic) as caught:
        a.encode({"x": "x"*a.MAX_OUTPUT})
    assert caught.value.stage == "output" and caught.value.reason == "output_limit"
