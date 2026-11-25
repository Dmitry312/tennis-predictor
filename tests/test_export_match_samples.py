"""Entirely fabricated fixtures; no production access or actual sports records."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("real_sample_export", ROOT/"scripts/export_match_samples.py")
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"
LIMITS = m.Limits()
SPORT = {"canonical_match_id": A, "player1": "Fixture Игрок A", "player2": "Fixture B",
         "nbbet_match_id": 123, "odds_open_p1": 1.935, "odds_open_p2": 2.01,
         "raw_json": '{"sets":{"1":{"score":6}},"players":{"123":"Fixture Игрок A"}}',
         "nullable": None, "version": 3}


class PG:
    def __init__(self, rows=None, readonly="on", timeout="5s", plan=None):
        self.rows = rows if rows is not None else {A: {"match_id": A, "player1": "Fixture Игрок A",
            "odds_open_p1": Decimal("1.935"), "pbp_completeness": Decimal("1.0")}}
        self.readonly, self.timeout, self.plan = readonly, timeout, plan
        self.sql = []
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query, params=None):
        self.sql.append((query, params))
        if "current_setting" in query: self.result = [(self.readonly,self.timeout)]
        elif query.startswith("EXPLAIN"):
            self.result = [([{"Plan": self.plan or {"Node Type":"Index Scan", "Relation Name":"matches",
                "Total Cost":40, "Index Cond":"match_date >= $1", "Plan Rows":5}}],)]
        elif query.startswith("SELECT match_id::text, row_to_json"):
            self.result = [(key,m.dumps(row).decode()) for key,row in self.rows.items() if key in params[0]]
        elif query.startswith("SELECT column_name"):
            self.result = [("match_id","uuid","uuid",None,None),("odds_open_p1","numeric","numeric",7,3)]
        elif query.startswith("SELECT table_name"):
            self.result = [("legacy_links","match_id")]
        elif query.startswith("SELECT match_id::text FROM"): self.result = [(A,),(B,)]
        else: raise AssertionError(query)
    def fetchmany(self, size):
        assert size <= 130
        return self.result[:size]


class Events:
    def __init__(self): self.hooks = {}
    def register(self, name, callback): self.hooks[name] = callback


class Missing(Exception):
    response = {"ResponseMetadata":{"HTTPStatusCode":404}}


class S3:
    def __init__(self, objects):
        self.objects = objects
        self.meta = SimpleNamespace(events=Events())
        self.calls = []
        self.streams = []
    def head_object(self, **params):
        self.calls.append(("HEAD", params))
        if params["Key"] not in self.objects: raise Missing()
        return {"ContentLength":len(self.objects[params["Key"]]), "ETag":'"fixture"',
                "LastModified":datetime(2026,1,1,tzinfo=timezone.utc)}
    def get_object(self, **params):
        self.calls.append(("GET",params))
        data = self.objects[params["Key"]]
        body = io.BytesIO(data); self.streams.append(body)
        return {"ContentLength":len(data), "ETag":'"fixture"', "Body":body,
                "ResponseMetadata":{"HTTPStatusCode":200}, "VersionId":params.get("VersionId")}


def parquet(rows):
    target = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(rows),target)
    return target.getvalue().to_pybytes()


def manifest(sources=None):
    return {"manifest_version":1,"matches":{A:sources or {}}}


def packages(row=None):
    pg = {A: row or {"match_id":A, "player1":"Fixture Игрок A", "odds_open_p1":Decimal("1.935"),
                     "created_at":datetime(2026,1,1), "nullable":None}}
    return m.bundles([A],pg,[],{"candidates":[],"truncated":False},{},
                     {"mode":"exact_uuid","truncated":False},LIMITS)


def test_full_export_preserves_sports_and_all_versions(tmp_path):
    sources = {s:[{"key":s+"/v1.parquet","version_id":"v1"},
                  {"key":s+"/v3.parquet","etag":'"fixture"'}] for s in m.SOURCES}
    objects = {s+"/"+version+".parquet":parquet([SPORT,dict(SPORT,canonical_match_id=B)])
               for s in m.SOURCES for version in ("v1","v3")}
    client = S3(objects)
    result = m.export(PG(),client,"sports.fixture.bucket",[A],manifest(sources),tmp_path,
                      LIMITS,{"mode":"exact_uuid"})
    assert result["published_matches"] == 1
    records = m.reuse(tmp_path,[A])[A]["records"]
    assert records["matches.json"]["rows"][0]["odds_open_p1"] == "1.935"
    for source in m.SOURCES:
        versions = records[source+".json"]["versions"]
        assert len(versions) == 2
        assert all(v["rows"] == [SPORT] for v in versions)
        assert versions[0]["schema"] and versions[0]["content_sha256"]
        assert records[source+".json"]["silent_version_selection"] is False
    assert len(client.calls) == 16
    assert all(p["Bucket"] == "sports.fixture.bucket" for _,p in client.calls)
    assert all(p["IfMatch"] == '"fixture"' for op,p in client.calls if op=="GET")
    assert all(s.closed for s in client.streams)
    assert all(q.lstrip().startswith(("SELECT","EXPLAIN")) for q,_ in PG().sql)


def test_pg_numeric_schema_and_inventory():
    db = PG()
    rows,schema,inventory = m.read_pg(db,[A],LIMITS)
    assert rows[A]["odds_open_p1"] == Decimal("1.935")
    assert schema[1]["numeric_scale"] == 3
    assert inventory["candidates"][0]["rows_exported"] is False
    assert "not_verified_fk" in inventory["candidates"][0]["basis"]
    assert all(q.startswith("SELECT") for q,_ in db.sql)


@pytest.mark.parametrize("readonly,timeout", [("off","5s"),("on","0"),("on","6s"),("on","1min")])
def test_fail_closed_db_session(readonly,timeout):
    with pytest.raises(m.ExportError): m.read_pg(PG(readonly=readonly,timeout=timeout),[A],LIMITS)


def test_exact_and_bounded_filter_selection():
    assert m.select_ids(PG(),[B,A,A],LIMITS)[0] == [A,B]
    ids,selection = m.select_ids(PG(),[],replace(LIMITS,matches=1),
                                date_from="2026-01-01",date_to="2026-01-02")
    assert ids == [A] and selection["truncated"] is True


@pytest.mark.parametrize("plan", [
    {"Node Type":"Seq Scan","Relation Name":"matches","Total Cost":10},
    {"Node Type":"Index Scan","Relation Name":"matches","Total Cost":10001,"Index Cond":"match_date > $1"},
    {"Node Type":"Index Scan","Relation Name":"matches","Total Cost":10,"Filter":"player1 = $1"},
])
def test_filters_reject_seqscan_and_unbounded_pk_scan(plan):
    with pytest.raises(m.ExportError): m.select_ids(PG(plan=plan),[],LIMITS,player="Fixture")


@pytest.mark.parametrize("kwargs", [{},{"date_from":"2026-01-01"},{"date_to":"2026-01-01"},
                                   {"date_from":"2026-01-01","date_to":"2026-12-01"}])
def test_invalid_selection(kwargs):
    with pytest.raises((m.ExportError,ValueError)): m.select_ids(PG(),[],LIMITS,**kwargs)


def test_match_filters_are_parameters():
    db = PG()
    text = "Fixture' OR true --"
    m.select_ids(db,[],LIMITS,player=text,tournament="Fixture cup")
    assert all(text not in q for q,_ in db.sql)
    assert any(params and text in params for _,params in db.sql)


def test_missing_pg_and_objects_are_explicit(tmp_path):
    source = {"pbp":[{"key":"pbp/missing.parquet","current":True}]}
    m.export(PG(rows={}),S3({}),"fixture.bucket",[A],manifest(source),tmp_path,LIMITS,{})
    item = m.reuse(tmp_path)[A]
    assert item["records"]["matches.json"]["coverage"] == "row_missing"
    assert item["records"]["pbp.json"]["versions"][0]["coverage"] == "object_missing"
    assert item["records"]["nbbet_raw.json"]["coverage"] == "unplanned_or_unavailable"
    assert item["manifest"]["coverage_complete"] is False


def test_missing_join_column_is_not_fabricated(tmp_path):
    source = {"pbp":[{"key":"pbp/legacy.parquet","current":True}]}
    m.export(PG(),S3({"pbp/legacy.parquet":parquet([{"old_match_id":123,"score":"6:4"}])}),
             "fixture.bucket",[A],manifest(source),tmp_path,LIMITS,{})
    version = m.reuse(tmp_path)[A]["records"]["pbp.json"]["versions"][0]
    assert version["coverage"] == "join_column_missing" and version["rows"] == []


def test_explicit_truncation_and_no_pbp_completeness_inference(tmp_path):
    source = {"pbp":[{"key":"pbp/v3.parquet","current":True}]}
    m.export(PG(),S3({"pbp/v3.parquet":parquet([SPORT,SPORT,SPORT])}),
             "fixture.bucket",[A],manifest(source),tmp_path,replace(LIMITS,source_rows=1),{})
    item = m.reuse(tmp_path)[A]
    version = item["records"]["pbp.json"]["versions"][0]
    assert len(version["rows"]) == 1 and version["rows_truncated"] == 2
    assert version["matching_rows_observed"] == 3
    assert item["manifest"]["coverage_complete"] is False


@pytest.mark.parametrize("version", [{"key":"pbp/x.parquet"},{"key":"pbp/../x.parquet","current":True},
                                     {"key":"other/x.parquet","current":True}])
def test_manifest_must_select_version_explicitly(version):
    with pytest.raises(m.ExportError): m.object_plan(manifest({"pbp":[version]}),[A],LIMITS)


def test_guard_denies_list_write_foreign_key_or_version():
    client = S3({})
    planned = m.object_plan(manifest({"pbp":[{"key":"pbp/x.parquet","version_id":"1"}]}),[A],LIMITS)
    reader = m.S3Reader(client,"fixture.bucket",planned,m.Budget(LIMITS))
    for name,params in [
        ("ListObjectsV2",{"Bucket":"fixture.bucket"}),("PutObject",{"Bucket":"fixture.bucket"}),
        ("HeadObject",{"Bucket":"fixture.bucket","Key":"pbp/other.parquet","VersionId":"1"}),
        ("GetObject",{"Bucket":"fixture.bucket","Key":"pbp/x.parquet","VersionId":"2","IfMatch":"tag"}),
        ("GetObject",{"Bucket":"fixture.bucket","Key":"pbp/x.parquet","VersionId":"1"}),
    ]:
        with pytest.raises(m.ExportError): reader.guard(SimpleNamespace(name=name),params)


def test_http_guard_denies_redirect_and_retry():
    reader = m.S3Reader(S3({}),"fixture.bucket",{},m.Budget(LIMITS))
    with pytest.raises(m.ExportError):
        reader.before_send(SimpleNamespace(url="https://evil.invalid/"))
    reader.before_send(SimpleNamespace(url="https://storage.yandexcloud.net/fixture.bucket/key"))
    with pytest.raises(m.ExportError):
        reader.before_send(SimpleNamespace(url="https://storage.yandexcloud.net/fixture.bucket/key"))


@pytest.mark.parametrize("limit", ["object_bytes","total_bytes","file_rows","total_rows","uncompressed_bytes","output_bytes"])
def test_limits_fail_before_publication(tmp_path,limit):
    source = {"pbp":[{"key":"pbp/v3.parquet","current":True}]}
    with pytest.raises(m.ExportError):
        m.export(PG(),S3({"pbp/v3.parquet":parquet([SPORT,SPORT])}),"fixture.bucket",[A],
                 manifest(source),tmp_path/"output",replace(LIMITS,**{limit:1}),{})
    assert not (tmp_path/"output"/"index.json").exists()


@pytest.mark.parametrize("value", [
    {"password":"fake"}, {"raw_json":'{"api_key":"fake"}'}, {"notes":"postgresql://fake/db"},
    {"notes":"-----BEGIN RSA PRIVATE KEY-----"}, {"notes":"fixture@example.invalid"},
    {"notes":"https://fixture.invalid/?token=fake"}, {"session_id":"fake"},
    {"notes":"exact-known-fake-secret"}, {"email":"fake"}, {"phone":"fake"}, {"raw_json":'{"token":"broken'},
])
def test_sensitive_content_aborts_without_output(tmp_path,value):
    with pytest.raises(m.ExportError):
        m.publish(tmp_path/"output",packages(dict(value,match_id=A)),secrets=("exact-known-fake-secret",))
    assert not (tmp_path/"output").exists()


def test_deterministic_atomic_reuse_and_immutable_conflict(tmp_path):
    data = packages()
    m.publish(tmp_path,data)
    path = tmp_path/"samples"/("match_"+A)
    before = {p.name:p.read_bytes() for p in path.iterdir()}
    first_index = (tmp_path/"index.json").read_bytes()
    m.publish(tmp_path,data)
    assert first_index == (tmp_path/"index.json").read_bytes()
    assert before == {p.name:p.read_bytes() for p in path.iterdir()}
    assert not list(tmp_path.glob(".export-stage-*"))
    loaded = m.reuse(tmp_path,[A])[A]["records"]["matches.json"]["rows"][0]
    assert loaded["created_at"] == "2026-01-01 00:00:00" and loaded["nullable"] is None
    changed = packages({"match_id":A,"player1":"Changed fixture"})
    with pytest.raises(m.ExportError): m.publish(tmp_path,changed)
    assert first_index == (tmp_path/"index.json").read_bytes()


def test_synthetic_index_entry_is_preserved(tmp_path):
    (tmp_path/"index.json").write_text(json.dumps({"catalog_version":1,
        "entries":[{"path":"examples.synthetic.json","origin":"synthetic"}],
        "publication_status":"synthetic_only_no_real_samples_approved","real_examples":[]}))
    m.publish(tmp_path,packages())
    index = json.loads((tmp_path/"index.json").read_text())
    assert index["entries"][0]["origin"] == "synthetic"
    assert index["owner_public_sports_approval"] == "2026-11-25"
    assert "owner_approved" in index["publication_status"]


@pytest.mark.parametrize("file", ["matches.json","manifest.json"])
def test_tampering_rejected(tmp_path,file):
    m.publish(tmp_path,packages())
    target = tmp_path/"samples"/("match_"+A)/file
    target.write_bytes(target.read_bytes()+b" ")
    with pytest.raises(m.ExportError): m.reuse(tmp_path,[A])


@pytest.mark.parametrize("path", ["samples","index.json",".export.lock"])
def test_output_symlink_rejected(tmp_path,path):
    outside = tmp_path/"outside"
    outside.mkdir()
    root = tmp_path/"catalog"; root.mkdir()
    (root/path).symlink_to(outside)
    with pytest.raises(m.ExportError): m.publish(root,packages())
    assert not list(outside.iterdir())


def cfg():
    return {"POSTGRES_HOST":"fixture", "POSTGRES_PORT":"5432","POSTGRES_USER":"fixture",
            "POSTGRES_PASSWORD":"not-a-production-password","POSTGRES_DB":"fixture",
            "S3_ENDPOINT_URL":"https://storage.yandexcloud.net","S3_REGION":"ru-central1",
            "S3_BUCKET":"sports.fixture.bucket","S3_ACCESS_KEY_ID":"fake-access","S3_SECRET_ACCESS_KEY":"fake-secret"}


def test_literal_config_dotted_bucket_and_no_shell_execution(tmp_path):
    values = cfg()
    file = tmp_path/".env"
    file.write_text("\n".join(k+"='"+v+"'" for k,v in values.items()))
    assert m.config(file,{}) == values
    with pytest.raises(m.ExportError): m.config(file,{"POSTGRES_HOST":"different"})
    values["POSTGRES_PASSWORD"] = "$(danger)"
    file.write_text("\n".join(k+"='"+v+"'" for k,v in values.items()))
    with pytest.raises(m.ExportError): m.config(file,{})


@pytest.mark.parametrize("changes", [{"S3_BUCKET":"127.0.0.1"},{"S3_BUCKET":"a..b"},
    {"S3_ENDPOINT_URL":"https://evil.invalid"},{"S3_REGION":"other"},{"POSTGRES_PORT":"0"}])
def test_invalid_config(changes):
    with pytest.raises(m.ExportError): m.config(environ=dict(cfg(),**changes))


def test_cli_reuse_needs_no_credentials_or_network(tmp_path,monkeypatch,capsys):
    m.publish(tmp_path,packages())
    monkeypatch.setattr(m,"config",lambda *a,**k:pytest.fail("credentials accessed"))
    assert m.main(["--reuse",str(tmp_path),"--match-id",A]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reused_matches"] == 1 and result["production_access"] is False


def test_cli_errors_do_not_echo_values_or_traceback(capsys):
    assert m.main(["--not-a-real-flag","fake-sensitive-argument"]) == 1
    captured = capsys.readouterr()
    assert "fake-sensitive-argument" not in captured.err and "Traceback" not in captured.err
    assert json.loads(captured.err)["data_printed"] is False


def test_uuid_and_json_representation():
    assert m.mid(uuid.UUID(A)) == A
    assert json.loads(m.dumps({"date":datetime(2026,1,1),"id":uuid.UUID(A),"odds":Decimal("2.001")})) == {
        "date":"2026-01-01 00:00:00","id":A,"odds":"2.001"}
    with pytest.raises(m.ExportError): m.read_json(__file__,3)


@pytest.mark.parametrize("field",list(m.asdict(LIMITS)))
def test_limits_have_hard_caps(field):
    with pytest.raises(m.ExportError): replace(LIMITS,**{field:10**12}).validate()
