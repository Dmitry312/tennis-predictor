import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("linked_review", ROOT/"infra/audit/linked_record_review.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
MID = str(uuid.UUID(int=1))
PATH = "pbp/gender=M/level=tour/year=2026/part-synthetic.parquet"


def plan():
    return {MID: a.exact_keys(PATH)}


def metadata():
    return {MID: {"match_id": MID, "gender": "M", "level": "tour", "status": 3,
                  "surface": "hard", "duplicate_of": None, "pbp_s3_path": "PRIVATE",
                  "player1": "PRIVATE NAME", "odds_open_p1": 777}}


def fixture(source, rows=1):
    private = {"1": "PRIVATE NAME", "2": "https://PRIVATE.invalid/photo", "7": 987654321}
    data = {
        "nbbet_details": {"status": 3, "has_coverage": None, "details_version": 3,
            "sets_json": json.dumps([{"1": [6, "4"], "2": None, "3": False}]),
            "players_json": json.dumps({"p1": private, "p2": private}), "extra_json": "PRIVATE"},
        "nbbet_stats": {"scope": 0, "aces_p1": 3, "aces_p2": None, "winners_p1": 1.5},
        "pbp": {"set_num": 1, "game_num": 1, "point_num": 1, "server": 1,
            "point_winner": 2, "score_server": "15", "score_returner": "0",
            "game_complete": False, "point_type": "PRIVATE"},
        "nbbet_raw": {"raw_json": json.dumps({"match": {"3": private, "5": private,
            "23": 3, "24": [{"1": [6, 4], "2": None}], "30": {"1": 777.0}},
            "PRIVATE": {"SECRET": "PRIVATE"}})},
    }[source]
    records = [{"canonical_match_id": MID, **data} for _ in range(rows)]
    stream = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(records), stream)
    return stream.getvalue()


class Fake:
    def __init__(self, missing=None, bad_header=False):
        self.missing, self.bad_header = missing, bad_header
        self.calls, self.bodies, self.events = [], [], []
        self.meta = SimpleNamespace(events=SimpleNamespace(register=lambda *x: self.events.append(x)))
    def head_object(self, **kw):
        self.calls.append(("head", kw))
        if kw["Key"].startswith(self.missing or "never/"):
            from botocore.exceptions import ClientError
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 404},
                               "Error": {"Code": "PRIVATE"}}, "HeadObject")
        source = kw["Key"].split("/")[0]
        return {"ContentLength": len(fixture(source)), "ETag": '"tag"'}
    def get_object(self, **kw):
        self.calls.append(("get", kw))
        source = kw["Key"].split("/")[0]
        data = fixture(source)
        body = io.BytesIO(data)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(data)+int(self.bad_header),
                "ETag": '"tag"', "ResponseMetadata": {"HTTPStatusCode": 200}}


def read_objects(fake=None):
    fake = fake or Fake()
    budget = a.Budget()
    reader = a.ObjectReader(fake, "synthetic-bucket", plan(), budget)
    objects = {(s,k): reader.read(s,k) for s,k in reader.keys}
    return fake, budget, objects


def orientations(order="same"):
    return {(MID,s): {"order": order, "reviewed": True, "reference": "PRIVATE"}
            for s in a.SOURCES}


def test_linked_sanitized_export_and_types():
    fake, budget, objects = read_objects()
    report = a.build_review(metadata(), objects, plan(), orientations=orientations(),
                            requested_cases=["finished_straight", "current"])
    data = a.encode(report).decode()
    for secret in (MID, "PRIVATE", "987654321", "https://", "part-synthetic", "777"):
        assert secret not in data
    entry = report["matches"][0]
    assert entry["match_alias"] == "M1" and entry["player_aliases"] == ["M1P1","M1P2"]
    d = entry["sources"]["nbbet_details"]["rows"][0]
    assert d["sets_json"][0]["fields"]["1"] == [6,"4"]
    assert d["sets_json"][0]["fields"]["2"] is None
    assert d["sets_json"][0]["fields"]["3"] is False
    assert entry["sources"]["nbbet_stats"]["rows"][0]["winners_p1"] == 1.5
    assert d["players_json"]["p1"]["player_alias"] == "M1P1"
    raw = entry["sources"]["nbbet_raw"]["rows"][0]["raw_json"]["match"]
    assert raw["3"]["player_alias"] == "M1P1" and "30" not in raw
    assert not entry["independent_winner"]["available"]
    assert budget.calls == 8 and all(b.closed for b in fake.bodies)


def test_unknown_and_reversed_orientation():
    _, _, objects = read_objects()
    report = a.build_review(metadata(), objects, plan())
    assert report["matches"][0]["sources"]["pbp"]["source_player_aliases"] is None
    report = a.build_review(metadata(), objects, plan(), orientations=orientations("swapped"))
    assert report["matches"][0]["sources"]["nbbet_details"]["rows"][0]["players_json"]["p1"]["player_alias"] == "M1P2"


def test_missing_legacy_and_null_schema():
    fake, _, objects = read_objects(Fake(missing="nbbet_raw/"))
    key = plan()[MID]["nbbet_details"]
    objects[("nbbet_details", key)] = {"rows": [{"canonical_match_id": MID, "status": 3, "sets_json": None}],
                                     "storage_types": {"status": "int", "sets_json": "null"}}
    report = a.build_review(metadata(), objects, plan())
    entry = report["matches"][0]
    assert entry["sources"]["nbbet_raw"]["coverage"] == "missing_or_unplanned"
    assert "players_json" in entry["sources"]["nbbet_details"]["absent_columns"]
    assert entry["sources"]["nbbet_details"]["rows"][0]["sets_json"] is None
    assert a.project("nbbet_raw", {"raw_json": None}, None) == {"raw_json": None}
    assert a.project("nbbet_raw", {"raw_json": "x"*4097}, None)["raw_json"]["omitted"] == "json_limit"


def test_row_caps_and_exact_joins():
    _, _, objects = read_objects()
    source = "pbp"
    key = plan()[MID][source]
    rows = [{"canonical_match_id": MID, "point_num": i} for i in range(9)]
    rows.append({"canonical_match_id": str(uuid.UUID(int=2)), "point_num": 99})
    rows.append({"canonical_match_id": "PRIVATE"})
    objects[(source,key)] = {"rows": rows, "storage_types": {"point_num": "int"}}
    projected = a.build_review(metadata(), objects, plan())["matches"][0]["sources"][source]
    assert [r["point_num"] for r in projected["rows"]] == list(range(6))
    assert projected["rows_omitted_by_cap"] == 3 and projected["invalid_join_rows"] == 1


def test_independent_reference_never_fabricated():
    _, _, objects = read_objects()
    evidence = {MID: {"basis": "independent_reference", "reviewed": True, "side": 2, "reference": "PRIVATE URL"}}
    r = a.build_review(metadata(), objects, plan(), independent_winners=evidence)
    winner = r["matches"][0]["independent_winner"]
    assert winner["winner_alias"] == "M1P2" and winner["verified_by_exporter"] is False
    assert "PRIVATE" not in a.encode(r).decode()
    evidence[MID]["basis"] = "score_derived"
    with pytest.raises(a.ReviewError):
        a.build_review(metadata(), objects, plan(), independent_winners=evidence)


@pytest.mark.parametrize("raw", ['bad', '[{"1":1,"1":2}]', "x"*4097])
def test_malformed_json_fail_closed(raw):
    with pytest.raises(a.ReviewError):
        a.structural_sets(raw)


def test_key_allowlist_and_scalar_redaction():
    shape = a.structural_sets('[{"1":[6,"4"],"PRIVATE":"NAME","2":123456789,"3":"https://PRIVATE","4":null}]')
    output = json.dumps(shape)
    assert "PRIVATE" not in output and "123456789" not in output
    assert list(shape[0]["fields"]) == ["1","2","3","4"]
    assert a.primitive(True) == {"redacted": "type"}
    assert a.primitive(10**1000) == {"redacted": "type"}


def test_deadlines_request_output_and_shape_caps():
    now = [0]
    b = a.Budget(clock=lambda: now[0])
    now[0] = 65
    with pytest.raises(a.ReviewError): b.charge()
    b = a.Budget()
    b.calls = a.MAX_CALLS
    with pytest.raises(a.ReviewError): b.charge()
    b = a.Budget()
    b.bytes = a.MAX_BYTES
    with pytest.raises(a.ReviewError): b.charge(1)
    with pytest.raises(a.ReviewError): a.encode({"x":"x"*a.MAX_OUTPUT})
    with pytest.raises(a.ReviewError): a.structural_sets([{}]*6)
    with pytest.raises(a.ReviewError): a.structural_sets([{"1":list(range(9))}])
    with pytest.raises(a.ReviewError): a.validate_plan({str(uuid.UUID(int=i)):{} for i in range(4)})


def test_operations_destinations_and_ignored_header():
    f = Fake(bad_header=True)
    r = a.ObjectReader(f, "synthetic-bucket", plan(), a.Budget())
    source, key = r.keys[0]
    with pytest.raises(a.ReviewError): r.read(source,key)
    assert f.bodies[-1].closed
    for op in ("put_object","list_objects_v2","delete_object"):
        with pytest.raises(a.ReviewError): r.call(op, Key=key)
    with pytest.raises(a.ReviewError):
        r.before_send(SimpleNamespace(url="https://evil.invalid/PRIVATE"))
    r.before_send(SimpleNamespace(url="https://storage.yandexcloud.net/synthetic"))
    with pytest.raises(a.ReviewError):
        r.before_send(SimpleNamespace(url="https://storage.yandexcloud.net/synthetic"))
    assert a.exact_keys("pbp/gender=M/level=itf/year=2026/part-x.parquet") is None


def test_oversized_parquet_and_duplicate_exclusion():
    class Large(Fake):
        def head_object(self, **kw):
            return {"ContentLength": a.MAX_FILE+1, "ETag":"tag"}
    f = Large()
    reader = a.ObjectReader(f,"synthetic-bucket",plan(),a.Budget())
    with pytest.raises(a.ReviewError): reader.read(*reader.keys[0])
    assert not f.bodies
    rows = metadata()
    rows[MID]["duplicate_of"] = "PRIVATE"
    with pytest.raises(a.ReviewError): a.build_review(rows,{},plan())


class Cursor:
    def __init__(self, readonly="on", timeout="3s"):
        self.queries = []
        self.readonly, self.timeout = readonly, timeout
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,sql,params=None): self.queries.append((sql,params))
    def fetchmany(self,n):
        if len(self.queries) == 1: return [(self.readonly,self.timeout)]
        return [(MID,"M","tour",3,"hard",None,PATH)]


def test_select_only_db_projection_and_timeout():
    cursor = Cursor()
    connection = SimpleNamespace(cursor=lambda:cursor)
    assert MID in a.read_matches(connection,[MID])
    assert all(sql.startswith("SELECT ") for sql,_ in cursor.queries)
    assert all("row_to_json" not in sql and "player1" not in sql and "odds" not in sql for sql,_ in cursor.queries)
    for readonly,timeout in [("off","3s"),("on","0"),("on","30s")]:
        c = Cursor(readonly,timeout)
        with pytest.raises(a.ReviewError):
            a.read_matches(SimpleNamespace(cursor=lambda:c),[MID])
        assert len(c.queries) == 1


def test_orchestrator_sanitizes_errors_and_does_not_print(monkeypatch,capsys):
    class Context:
        def __enter__(self): return None
        def __exit__(self,*args): return False
    monkeypatch.setattr(a,"isolated_limits",lambda:Context())
    cursor = Cursor()
    payload = a.review_from_clients(SimpleNamespace(cursor=lambda:cursor),Fake(),"synthetic-bucket",plan())
    assert json.loads(payload)["matches"][0]["match_alias"] == "M1"
    def fail(*args): raise ValueError("PRIVATE credentials object-key")
    monkeypatch.setattr(a,"read_matches",fail)
    with pytest.raises(a.ReviewError,match="^linked_review_unavailable$"):
        a.review_from_clients(None,None,"synthetic-bucket",plan())
    assert capsys.readouterr().out == "" and capsys.readouterr().err == ""


def test_synthetic_catalog_examples_are_separate_and_bounded():
    catalog = ROOT/"docs/ml/data_catalog"
    index = json.loads((catalog/"index.json").read_text())
    assert any(entry.get("path")=="examples.synthetic.json" and entry.get("origin")=="synthetic"
               for entry in index["entries"])
    # The persistent catalog may now contain real batches; fiction stays a separate origin.
    assert all(entry.get("path")!="examples.synthetic.json" and entry.get("origin")=="real_source_records"
               for entry in index["real_examples"])
    sample = json.loads((catalog/"examples.synthetic.json").read_text())
    assert sample["origin"] == "synthetic_fabricated_not_production"
    for batch in sample["batches"]:
        assert len(batch["matches"]) <= a.MAX_MATCHES and batch["complete"] is False
        for match in batch["matches"]:
            assert match["independent_winner"]["available"] is False
            for source in ("nbbet_details","nbbet_raw"):
                for row in match["sources"][source]["rows"]:
                    assert "canonical_match_id" not in row
    assert "not a production mapping" in sample["schema_semantics"]
    text = (catalog/"examples.synthetic.json").read_text()
    import re
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)
    assert "https://" not in text


def test_new_workflow_has_no_production_secrets_or_dispatch():
    import yaml
    import subprocess
    path = ROOT/".github/workflows/linked-record-review-tests.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"pull_request"}
    assert "secrets." not in path.read_text()
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert len(step["uses"].split("@")[1]) == 40
            if "run" in step:
                subprocess.run(["bash","-n"],input=step["run"],text=True,check=True)


def test_source_schema_and_projection_type_boundaries():
    r = a.project("pbp", {"server": True, "point_winner": 4, "score_server": "PRIVATE",
                         "point_type": "unrecognized", "odds_open_p1": 2.0}, None)
    assert "odds_open_p1" not in r and "PRIVATE" not in json.dumps(r)
    assert r["server"]["redacted"] == "type"
    assert r["point_winner"]["redacted"] == "side"
    assert a.project("nbbet_stats", {"scope": 0, "aces_p1": None}, None) == {"scope":0,"aces_p1":None}
    with pytest.raises(a.ReviewError):
        a.project("nbbet_raw", {"raw_json":"not json"}, None)
    with pytest.raises(a.ReviewError):
        a.build_review(metadata(), {}, plan(), requested_cases=["PRIVATE"])


def test_decode_row_limit_and_corrupt_file():
    class Corrupt(Fake):
        def head_object(self, **kw):
            return {"ContentLength": 16, "ETag":"tag"}
        def get_object(self, **kw):
            b = io.BytesIO(b"PAR1xxxxxxxxPAR1")
            self.bodies.append(b)
            return {"ContentLength":16,"ETag":"tag","Body":b,
                    "ResponseMetadata":{"HTTPStatusCode":200}}
    fake = Corrupt()
    reader = a.ObjectReader(fake,"synthetic-bucket",plan(),a.Budget())
    with pytest.raises(a.ReviewError):
        reader.read(*reader.keys[0])
    assert fake.bodies[0].closed
    objects = {("pbp",plan()[MID]["pbp"]):{"rows":[{}]*2049,"storage_types":{}}}
    with pytest.raises(a.ReviewError):
        a.build_review(metadata(), objects, plan())


def test_isolated_resource_limits_restore(monkeypatch):
    import resource
    calls = []
    monkeypatch.setattr(resource,"getrlimit",lambda *x:(resource.RLIM_INFINITY,resource.RLIM_INFINITY))
    monkeypatch.setattr(resource,"setrlimit",lambda *x:calls.append(x))
    monkeypatch.setattr(a.signal,"getsignal",lambda *x:a.signal.SIG_DFL)
    monkeypatch.setattr(a.signal,"signal",lambda *x:None)
    monkeypatch.setattr(a.signal,"alarm",lambda *x:0)
    with a.isolated_limits():
        pass
    assert calls[0][1][0] == 4*1024**3
    assert calls[-1][1] == (resource.RLIM_INFINITY,resource.RLIM_INFINITY)
