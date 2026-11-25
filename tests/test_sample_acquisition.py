"""Fabricated offline fixtures; never production semantic evidence."""
from datetime import datetime, timezone, timedelta
import io
import json
from pathlib import Path
import subprocess
import sys
import uuid
import zipfile
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import acquire_match_samples as a
from scripts import export_match_samples as e
from scripts import sample_delivery as d
from scripts import build_sample_acquisition_payload as b
from infra.audit import sample_acquisition_bootstrap as bootstrap
from test_export_match_samples import PG, S3, parquet, SPORT

SHA = "a"*40
PATH = "pbp/gender=M/level=tour/year=2026/part-fixture.parquet"


def rows(count=40):
    return [(str(uuid.UUID(int=i+1)),("M","F")[i%2],("tour","challenger")[i%2],
             f"{2024+i%3}-01-01",("hard","clay","grass")[i%3],(3,9,10)[i%3],i%3+1,
             bool(i%2),PATH,None) for i in range(count)]


class CandidatePG(PG):
    def __init__(self, candidates=None, candidate_plan=None):
        self.candidates = rows() if candidates is None else candidates
        self.candidate_plan = candidate_plan or [{"Plan":{"Node Type":"Limit","Total Cost":40,
            "Plans":[{"Node Type":"Index Scan","Relation Name":"matches","Index Name":"matches_pkey"}]}}]
        pg = {r[0]:{"match_id":r[0],"gender":r[1],"level":r[2],"match_date":r[3],
                     "surface":r[4],"status":r[5],"details_version":r[6],"has_pbp":r[7],
                     "pbp_s3_path":r[8],"duplicate_of":r[9]} for r in self.candidates}
        super().__init__(rows=pg)
    def execute(self,query,params=None):
        if "SELECT match_id::text, gender" in query:
            self.sql.append((query,params))
            if query.startswith("EXPLAIN"): self.result = [(self.candidate_plan,)]
            else:
                after = params[0] if len(params)==2 else None
                self.result = [r for r in self.candidates if after is None or r[0]>after][:a.PAGE_SIZE+1]
        else: super().execute(query,params)
    def fetchmany(self,n):
        assert n<=a.PAGE_SIZE+2
        return self.result[:n]


def objects(ids):
    suffix = PATH[len("pbp/"):]
    data = parquet([dict(SPORT,canonical_match_id=key) for key in ids])
    return {source+"/"+suffix:data for source in e.SOURCES}


def test_deterministic_diverse_first20():
    db=CandidatePG()
    pool,discovery=a.candidate_pool(db)
    plan=a.choose(pool,discovery)
    assert len(plan["selected_ids"])==20 and plan["shortfall"]==0
    assert plan==a.choose(list(reversed(pool)),discovery)
    selected=plan["selected_metadata"]
    assert {r["gender"] for r in selected}=={"M","F"}
    assert {r["level"] for r in selected}=={"tour","challenger"}
    assert len({r["year"] for r in selected})==3
    assert len({r["status"] for r in selected})==3
    assert plan["score_formats"]=="unknown_until_source_review"
    assert discovery["population_complete"] is False
    assert all(q.startswith(("SELECT","EXPLAIN")) for q,_ in db.sql)


def test_bounded_parameterized_keyset_pages():
    candidates=rows(600)
    db=CandidatePG(candidates)
    pool,report=a.candidate_pool(db,pages=2)
    assert len(pool)==256 and report["rows_examined"]==256
    selects=[(q,p) for q,p in db.sql if q.startswith("SELECT match_id::text, gender")]
    assert len(selects)==2 and "match_id > %s::uuid" in selects[1][0]
    assert selects[1][1][0]==candidates[127][0]
    pool,_=a.candidate_pool(CandidatePG(candidates),pages=1,after=candidates[299][0])
    assert pool[0]["match_id"]==candidates[300][0]


@pytest.mark.parametrize("node",[
    {"Node Type":"Seq Scan","Relation Name":"matches"},
    {"Node Type":"Sort"},
    {"Node Type":"Index Scan","Relation Name":"matches","Index Name":"other"},
    {"Node Type":"Index Scan","Relation Name":"matches","Index Name":"matches_pkey","Filter":"level=tour"},
])
def test_refuse_full_scan_sort_or_postfilter(node):
    with pytest.raises(e.ExportError):
        a.candidate_pool(CandidatePG(candidate_plan=[{"Plan":dict(node,**{"Total Cost":10})}]))


def test_shortfall_and_ineligible_are_explicit():
    sample=rows(6)
    sample[0]=(*sample[0][:2],"itf",*sample[0][3:])
    sample[1]=(*sample[1][:-1],sample[0][0])
    pool,discovery=a.candidate_pool(CandidatePG(sample))
    plan=a.choose(pool,discovery)
    assert len(plan["selected_ids"])==4 and plan["shortfall"]==16
    with pytest.raises(e.ExportError): a.choose([],discovery)


def test_twenty_matches_in_ten_resumed_batches(tmp_path):
    plan=a.choose(*a.candidate_pool(CandidatePG()))
    exported=[]
    for offset in range(0,20,2):
        output=tmp_path/str(offset)
        selected,report=a.acquire(CandidatePG(),S3(objects(plan["selected_ids"])),
            "sports.fixture.bucket",output,plan=plan,offset=offset,batch_size=2)
        assert selected==plan and report["exported_count"]==2
        assert report["plan_sha256"]==e.sha(e.dumps(plan))
        batch=e.reuse(output); exported.extend(batch)
        for bundle in batch.values():
            for source in e.SOURCES:
                assert bundle["records"][source+".json"]["versions"][0]["rows"]
                assert bundle["manifest"]["coverage_complete"] is False
    assert len(set(exported))==20 and set(exported)==set(plan["selected_ids"])


def test_default_initial_batch_two(tmp_path):
    db=CandidatePG()
    plan,report=a.acquire(db,S3(objects(db.rows)),"fixture.bucket",tmp_path)
    assert len(plan["selected_ids"])==20 and report["exported_count"]==2
    assert len(e.reuse(tmp_path))==2
    assert "exact_sibling_candidates_do_not_discover_all_history" in report["blockers"]


def test_missing_paths_and_other_pg_tables(tmp_path):
    sample=[(*row[:8],None,row[9]) for row in rows(4)]
    plan,report=a.acquire(CandidatePG(sample),S3({}),"fixture.bucket",tmp_path)
    assert len(report["current_pointer_unavailable_match_ids"])==2
    for bundle in e.reuse(tmp_path).values():
        assert bundle["records"]["nbbet_raw.json"]["coverage"]=="unplanned_or_unavailable"
    assert report["other_linked_pg_tables"]["candidates"][0]["rows_exported"] is False


def test_exact_reviewed_history_manifest():
    ids=[rows(1)[0][0]]
    plan,missing=a.source_manifest({},ids,{"manifest_version":1,"matches":{
        ids[0]:{"nbbet_raw":[{"key":"nbbet_raw/reviewed.parquet","version_id":"v1"},
                           {"key":"nbbet_raw/reviewed.parquet","version_id":"v2"}]}}})
    assert len(e.object_plan(plan,ids,e.Limits()))==2 and missing==ids


@pytest.mark.parametrize("kwargs",[{"offset":1},{"batch_size":6},{"pages":5},{"offset":20}])
def test_invalid_batch_no_output(tmp_path,kwargs):
    with pytest.raises(e.ExportError):
        a.acquire(CandidatePG(),S3({}),"fixture.bucket",tmp_path/"output",**kwargs)
    assert not (tmp_path/"output").exists()


def test_archive_exact_files(tmp_path):
    db=CandidatePG()
    plan,report=a.acquire(db,S3(objects(db.rows)),"fixture.bucket",tmp_path/"catalog")
    payload=a.make_archive(tmp_path/"catalog",plan,report)
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert len(z.namelist())==15
        assert set(("selection.json","acquisition-report.json","catalog/index.json"))<=set(z.namelist())
        assert not any(".export.lock" in p or ".env" in p for p in z.namelist())


@pytest.fixture(scope="module")
def recipient(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization,hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    root=tmp_path_factory.mktemp("recipient")
    key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"offline fixture")])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1).not_valid_before(now-timedelta(days=1))
        .not_valid_after(now+timedelta(days=30)).sign(key,hashes.SHA256()))
    cert_path=root/"recipient.pem"; cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path=root/"private.pem"; key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    return cert_path,key_path,cert.fingerprint(hashes.SHA256()).hex()


def test_authenticated_delivery_reuse_and_tamper(tmp_path,recipient):
    cert,key,fingerprint=recipient
    db=CandidatePG()
    plan,report=a.acquire(db,S3(objects(db.rows)),"fixture.bucket",tmp_path/"catalog")
    clear=a.make_archive(tmp_path/"catalog",plan,report)
    encrypted=tmp_path/"samples.cms"
    d.seal(io.BytesIO(clear),encrypted,cert,fingerprint)
    assert b"Fixture" not in encrypted.read_bytes()
    d.unpack(encrypted,tmp_path/"review",key)
    assert len(e.reuse(tmp_path/"review"/"catalog"))==2
    tampered=bytearray(encrypted.read_bytes()); tampered[-1]^=1
    encrypted.write_bytes(tampered)
    with pytest.raises(ValueError): d.unpack(encrypted,tmp_path/"bad",key)
    assert not (tmp_path/"bad").exists()


def test_recipient_mismatch(tmp_path,recipient):
    cert,_,_=recipient
    with pytest.raises(ValueError): d.seal(io.BytesIO(b"data"),tmp_path/"x",cert,"0"*64)
    assert not (tmp_path/"x").exists()


@pytest.mark.parametrize("name",["../escape.json","/escape.json","catalog/other.json"])
def test_archive_paths_cannot_escape(tmp_path,recipient,name):
    cert,key,fingerprint=recipient
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,"w") as z: z.writestr(name,"fixture")
    encrypted=tmp_path/"bad.cms"; d.seal(io.BytesIO(stream.getvalue()),encrypted,cert,fingerprint)
    with pytest.raises(ValueError): d.unpack(encrypted,tmp_path/"output",key)
    assert not (tmp_path/"output").exists()


def test_payload_reviewed_source_and_root_path_rejection():
    code=b.build("/srv/tennis-predictor",SHA,0,2,4)
    compile(code,"payload","exec")
    assert "raise SystemExit(main(" in code
    assert "SERVER_SSH_KEY" not in code
    for project in ("/root/Tennis_pelmen_model","/srv/../root","/srv/x'bad"):
        with pytest.raises(e.ExportError): b.build(project,SHA,0,2,4)


def test_resume_requires_plan_and_normalizes_empty_cursor(monkeypatch):
    with pytest.raises(e.ExportError): b.build("/srv/project",SHA,2,2,4)
    plan=a.choose(*a.candidate_pool(CandidatePG()))
    monkeypatch.setattr(b,"document",lambda _:plan)
    compile(b.build("/srv/project",SHA,2,2,4,after="",plan_path="fixture"),"resume","exec")


def test_bootstrap_root_rejected_before_config(monkeypatch):
    monkeypatch.setattr(bootstrap.os,"geteuid",lambda:0)
    with pytest.raises(ValueError): bootstrap.execute(b"{}")


def test_workflow_guardrails_and_shell_syntax():
    import yaml
    file=ROOT/".github/workflows/production-match-sample-acquisition.yml"
    text=file.read_text()
    wf=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(wf["on"])=={"workflow_dispatch"} and wf["permissions"]=={"contents":"read"}
    job=wf["jobs"]["acquire"]
    assert job["environment"]=="match-sample-review" and "refs/heads/ML" in job["if"]
    assert wf["on"]["workflow_dispatch"]["inputs"]["batch_size"]["default"]=="2"
    assert "StrictHostKeyChecking=yes" in text and "SERVER_HOST_FINGERPRINT" in text
    assert '!= root' in text and "/root/Tennis_pelmen_model" not in text
    assert "actions/upload-artifact@" in text and "/samples.cms" in text
    assert "git push" not in text and "sample-stage.err" not in text
    for step in job["steps"]:
        if "uses" in step: assert len(step["uses"].split("@")[1])==40
        if "run" in step: subprocess.run(["bash","-n"],input=step["run"],text=True,check=True)


def test_reviewed_import_preserves_hashes_and_synthetic_index(tmp_path,recipient):
    cert,key,fingerprint=recipient
    db=CandidatePG()
    plan,report=a.acquire(db,S3(objects(db.rows)),"fixture.bucket",tmp_path/"catalog")
    encrypted=tmp_path/"samples.cms"
    d.seal(io.BytesIO(a.make_archive(tmp_path/"catalog",plan,report)),encrypted,cert,fingerprint)
    d.unpack(encrypted,tmp_path/"review",key)
    target=tmp_path/"git-catalog"; target.mkdir()
    (target/"index.json").write_text(json.dumps({"catalog_version":2,"real_examples":[],
        "entries":[{"origin":"synthetic","path":"examples.synthetic.json"}]}))
    with pytest.raises(ValueError): d.import_reviewed(tmp_path/"review",target)
    d.import_reviewed(tmp_path/"review",target,content_reviewed=True,rights_reviewed=True)
    before=json.loads((tmp_path/"review"/"catalog"/"index.json").read_text())
    after=json.loads((target/"index.json").read_text())
    assert after["entries"]==[{"origin":"synthetic","path":"examples.synthetic.json"}]
    assert after["real_examples"]==before["real_examples"]
    assert len(e.reuse(target))==2
