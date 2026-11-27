from datetime import date
from types import SimpleNamespace
import io
import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError
from tennis_predictor.ingestion.nbbet import stats_crosscheck as m

PREFIX="nbbet_stats/gender=F/level=tour/year=2026/"
OLD=PREFIX+"part-details.parquet"
NEW=PREFIX+"part-new-crosscheck-1.parquet"

class S3:
    def __init__(self):
        self.objects={}; self.gets=[]; self.fail=None; self.list_fail=False
    def add(self,key,rows,tag):
        out=pa.BufferOutputStream(); pq.write_table(pa.Table.from_pylist(rows),out)
        self.objects[key]=(out.getvalue().to_pybytes(),tag)
    def get_paginator(self,name):
        assert name=="list_objects_v2"; return self
    def paginate(self,**kwargs):
        if self.list_fail: raise ClientError({"Error":{"Code":"AccessDenied"}},"ListObjectsV2")
        yield {"Contents":[{"Key":k,"ETag":v[1],"Size":len(v[0])} for k,v in sorted(self.objects.items())
                           if k.startswith(kwargs["Prefix"])]}
    def get_object(self,**kwargs):
        key=kwargs["Key"]; self.gets.append(key)
        if self.fail: raise ClientError({"Error":{"Code":self.fail}},"GetObject")
        raw,tag=self.objects[key]; assert kwargs["IfMatch"]==tag
        return {"Body":io.BytesIO(raw),"ETag":tag}
def row(value=1,stamp="2026-11-01 00:00:00",mid="a",scope=0):
    return dict(canonical_match_id=mid,scope=scope,fetched_at=stamp,aces_p1=value)
def matches(path="pbp/gender=F/level=tour/year=2026/part-0000.parquet",mid="a"):
    return [SimpleNamespace(match_id=mid,pbp_s3_path=path)]
@pytest.fixture(autouse=True)
def cache(tmp_path,monkeypatch):
    monkeypatch.setenv("TENNIS_STATS_INDEX_DIR",str(tmp_path/"index"))
def load(s3,items=None):
    return m.load_group(s3,"fixture","F","tour",2026,items or matches())

def test_absent_pbp_link_not_requested_stats_independent():
    s=S3(); s.add(OLD,[row()],"old")
    assert load(s)[("a",0)][1]["aces_p1"]==1
    assert s.gets==[OLD]
def test_null_pbp_and_missing_after_complete_search():
    s=S3(); s.add(OLD,[row()],"old")
    assert load(s,matches(None))[("a",0)][1]["aces_p1"]==1
    assert load(s,matches(None,"missing"))=={}
    assert s.gets==[OLD]
def test_repeated_days_and_process_reopen_do_not_redownload():
    s=S3(); s.add(OLD,[row(),row(mid="b")],"old")
    load(s); assert load(s,matches(mid="b"))[("b",0)]
    assert s.gets==[OLD]
def test_new_crosscheck_and_changed_object_refreshed():
    s=S3(); s.add(OLD,[row()],"old"); load(s)
    s.add(NEW,[row(2,"2026-11-02 00:00:00")],"new")
    assert load(s)[("a",0)][1]["aces_p1"]==2
    assert s.gets==[OLD,NEW]
    s.add(NEW,[row(3,"2026-11-03 00:00:00")],"changed")
    assert load(s)[("a",0)][1]["aces_p1"]==3
    assert s.gets==[OLD,NEW,NEW]
def test_deleted_version_falls_back_to_remaining_source():
    s=S3(); s.add(OLD,[row()],"old"); s.add(NEW,[row(2,"2026-11-02")],"new")
    load(s); del s.objects[NEW]
    assert load(s)[("a",0)][1]["aces_p1"]==1
@pytest.mark.parametrize("error",["AccessDenied","NoSuchKey","SlowDown"])
def test_infrastructure_failure_never_missing_and_recovery(error,caplog):
    s=S3(); s.add(OLD,[row()],"old"); s.fail=error
    with pytest.raises(ClientError): load(s)
    assert OLD in caplog.text
    s.fail=None
    assert load(s)[("a",0)][1]["aces_p1"]==1
    assert s.gets==[OLD,OLD]
def test_listing_failure_does_not_serve_stale_cache():
    s=S3(); s.add(OLD,[row()],"old"); load(s); s.list_fail=True
    with pytest.raises(ClientError): load(s)
    assert s.gets==[OLD]
def test_failure_after_one_object_rolls_back_whole_refresh(monkeypatch):
    s=S3(); s.add(OLD,[row()],"old"); s.add(NEW,[row(2,"2026-11-02")],"new")
    original=m.read_rows
    def read(client,bucket,key,etag):
        if key==NEW: raise OSError("fixture interrupted")
        return original(client,bucket,key,etag)
    monkeypatch.setattr(m,"read_rows",read)
    with pytest.raises(OSError): load(s)
    monkeypatch.setattr(m,"read_rows",original)
    assert load(s)[("a",0)][1]["aces_p1"]==2
def test_fatal_s3_failure_does_not_create_day_checkpoint(tmp_path,monkeypatch):
    from tennis_predictor.ingestion.nbbet import stats_crosscheck_backfill as b
    def broken(**kw): raise ClientError({"Error":{"Code":"AccessDenied"}},"GetObject")
    monkeypatch.setattr(b,"reconcile",broken)
    state=tmp_path/"markers"
    with pytest.raises(ClientError): b.run(date(2026,10,28),date(2026,10,28),state,apply=True)
    assert not list(state.glob("*.json"))
def test_existing_partial_and_success_markers_preserved(tmp_path,monkeypatch):
    from tennis_predictor.ingestion.nbbet import stats_crosscheck_backfill as b
    (tmp_path/"2026-10-29.apply.json").write_text('{"complete":true}')
    (tmp_path/"2026-10-28.partial.json").write_text('{"complete":false}')
    before={p.name:p.read_bytes() for p in tmp_path.glob("*.json")}
    calls=[]
    monkeypatch.setattr(b,"reconcile",lambda **kw:calls.append(kw["day"]) or {"complete":True})
    b.run(date(2026,10,27),date(2026,10,29),tmp_path,apply=True,reverse=True)
    assert calls==[date(2026,10,27)]
    assert all((tmp_path/n).read_bytes()==v for n,v in before.items())
