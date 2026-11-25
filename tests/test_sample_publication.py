"""Offline publication/staging tests. No production or GitHub writes."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import export_match_samples as e
from scripts import acquire_match_samples as a
from scripts import review_staging as stage
from scripts import publish_match_samples as p
from scripts import sample_workflow_capture as capture
from scripts import sample_workflow_request as request
from scripts import build_sample_acquisition_payload as builder
from tests.test_sample_acquisition import CandidatePG, objects, S3

SHA="a"*40
TICKET="gh-100-1"


@pytest.fixture
def sample(tmp_path):
    db=CandidatePG()
    plan,report=a.acquire(db,S3(objects(db.rows)),
        "fixture.bucket",tmp_path/"catalog")
    report["source_commit"]=SHA
    data=a.make_archive(tmp_path/"catalog",plan,report)
    approval={"content_reviewed":True,"rights_reviewed":True,"content_evidence":"fixture content review 1",
              "rights_evidence":"fixture source terms review 1","archive_sha256":e.sha(data)}
    checkout=tmp_path/"checkout"
    catalog=checkout/"docs/ml/data_catalog"; catalog.mkdir(parents=True)
    (catalog/"index.json").write_text(json.dumps({"catalog_version":2,"real_examples":[],
        "entries":[{"origin":"synthetic","path":"examples.synthetic.json"}]}))
    return data,approval,checkout


class API:
    token="fake-publication-token"
    def __init__(self): self.calls=[]
    def call(self,method,path,data=None):
        self.calls.append((method,path,data))
        if method=="GET":
            if path=="git/ref/heads/ML": return {"object":{"sha":SHA}}
            if path.startswith("git/ref/heads/samples/"): return None
            if path=="git/commits/"+SHA: return {"tree":{"sha":"base-tree"}}
        if path=="git/trees": return {"sha":"sample-tree"}
        if path=="git/commits": return {"sha":"sample-commit"}
        if path=="git/refs": return {}
        if path=="pulls": return {"number":999}
        pytest.fail("unexpected endpoint")


def test_private_stage_release_cleanup(sample,tmp_path):
    data,approval,_=sample
    root=tmp_path/"private"
    metadata=stage.store(data,TICKET,SHA,root=root)
    assert metadata["archive_sha256"]==e.sha(data)
    assert (root/TICKET/"review/catalog/index.json").exists()
    assert (root/TICKET).stat().st_mode & 0o777==0o700
    assert (root/TICKET/"payload.zip").stat().st_mode & 0o777==0o600
    assert stage.release(TICKET,e.sha(data),SHA,approval,root=root)==data
    stage.cleanup(TICKET,root=root)
    assert not (root/TICKET).exists()


@pytest.mark.parametrize("change",[{"content_reviewed":False},{"rights_reviewed":False},
    {"content_evidence":""},{"rights_evidence":""},{"archive_sha256":"0"*64}])
def test_fail_closed_before_any_git_write(sample,change):
    data,approval,checkout=sample
    api=API()
    with pytest.raises((ValueError,e.ExportError)):
        p.publish(api,data,checkout,SHA,TICKET,dict(approval,**change))
    assert not api.calls


def test_review_secret_reference_blocked_before_git(sample):
    data,approval,checkout=sample
    api=API()
    approval["content_evidence"]="postgresql://fixture:secret@private/db"
    with pytest.raises(e.ExportError): p.publish(api,data,checkout,SHA,TICKET,approval)
    assert not api.calls


def test_automated_pr_uses_only_reviewed_json_and_preserves_synthetic(sample):
    data,approval,checkout=sample
    api=API()
    assert p.publish(api,data,checkout,SHA,TICKET,approval)=={"number":999}
    tree=next(v for method,path,v in api.calls if path=="git/trees")
    assert tree["base_tree"]=="base-tree"
    paths={item["path"] for item in tree["tree"]}
    assert len(paths)==15 and all(v.startswith("docs/ml/data_catalog/") and v.endswith(".json") for v in paths)
    assert not any(".env" in v or "key.pem" in v for v in paths)
    index=json.loads(next(v["content"] for v in tree["tree"] if v["path"].endswith("/index.json")))
    assert index["entries"]==[{"origin":"synthetic","path":"examples.synthetic.json"}]
    assert len(index["real_examples"])==2
    ref=next(v for _,path,v in api.calls if path=="git/refs")
    assert ref["ref"]=="refs/heads/samples/first20-"+TICKET
    pr=next(v for _,path,v in api.calls if path=="pulls")
    assert pr["base"]=="ML" and "merge" not in pr
    assert not any("/merge" in path or method=="PATCH" for method,path,_ in api.calls)


def test_changed_ml_blocks_publication(sample):
    data,approval,checkout=sample
    class Moved(API):
        def call(self,method,path,data=None):
            if path=="git/ref/heads/ML": return {"object":{"sha":"b"*40}}
            return super().call(method,path,data)
    api=Moved()
    with pytest.raises(e.ExportError,match="ml_changed"):
        p.publish(api,data,checkout,SHA,TICKET,approval)
    assert not any(method=="POST" for method,_,_ in api.calls)


def test_stage_changed_expired_and_sha_mismatch_rejected(sample,tmp_path,monkeypatch):
    data,approval,_=sample
    root=tmp_path/"private"
    stage.store(data,TICKET,SHA,root=root)
    with pytest.raises(e.ExportError):
        stage.release(TICKET,e.sha(data),"b"*40,approval,root=root)
    monkeypatch.setattr(stage.time,"time",lambda:time.time_ns()/1e9+stage.TTL+5)
    with pytest.raises(e.ExportError,match="stage_expired"):
        stage.release(TICKET,e.sha(data),SHA,approval,root=root)
    # Expired stages can still be explicitly cleaned.
    stage.cleanup(TICKET,root=root)


def test_capacity_and_symlink_fail_closed(sample,tmp_path):
    data,_,_=sample
    root=tmp_path/"private"
    stage.store(data,"gh-1-1",SHA,root=root)
    stage.store(data,"gh-2-1",SHA,root=root)
    with pytest.raises(e.ExportError,match="stage_capacity"):
        stage.store(data,"gh-3-1",SHA,root=root)
    outside=tmp_path/"outside"; outside.mkdir()
    linked=tmp_path/"linked"; linked.symlink_to(outside)
    with pytest.raises(e.ExportError): stage.store(data,"gh-4-1",SHA,root=linked)
    assert not list(outside.iterdir())


def test_capture_never_prints_raw_content(sample,tmp_path,capsys):
    data,approval,_=sample
    summary=tmp_path/"summary"
    environ={"OPERATION":"stage","STAGE_TICKET":TICKET,"REVIEWED_SHA":SHA,"GITHUB_STEP_SUMMARY":str(summary)}
    metadata={"ticket":TICKET,"source_sha":SHA,"archive_sha256":e.sha(data),
              "created_at":1,"exported_count":2}
    capture.capture(io.BytesIO(json.dumps(metadata).encode()),environ)
    assert "Fixture" not in summary.read_text() and "player" not in summary.read_text()
    assert e.sha(data) in summary.read_text()
    assert capsys.readouterr().out==""
    with pytest.raises(ValueError):
        capture.capture(io.BytesIO(data),environ)


def test_existing_root_reuse_explicit_not_new_permission():
    code=builder.build("/root/Tennis_pelmen_model",SHA,0,2,4,allow_existing_root=True)
    compile(code,"reviewed-root-reuse","exec")
    with pytest.raises(e.ExportError): builder.build("/root/Tennis_pelmen_model",SHA,0,2,4)


def test_release_request_requires_review_before_ssh(sample):
    _,approval,_=sample
    env={"OPERATION":"release","REVIEWED_SHA":SHA,"SELECTED_SHA":SHA,"STAGE_TICKET":TICKET,
         "PROJECT_PATH":"/srv/existing","STAGE_DIGEST":approval["archive_sha256"],
         "CONTENT_REVIEWED":"true","RIGHTS_REVIEWED":"true",
         "CONTENT_EVIDENCE":approval["content_evidence"],"RIGHTS_EVIDENCE":approval["rights_evidence"]}
    options=request.parameters(env)
    assert options["operation"]=="release" and options["approval"]==approval
    env["RIGHTS_REVIEWED"]="false"
    with pytest.raises(e.ExportError): request.parameters(env)


def test_workflow_token_isolation_and_optional_crypto():
    import yaml
    text=(ROOT/".github/workflows/production-match-sample-acquisition.yml").read_text()
    workflow=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(workflow["on"])=={"workflow_dispatch"}
    assert "cryptography" not in text and "RECIPIENT" not in text and "upload-artifact" not in text
    acquire=workflow["jobs"]["acquire"]; publish=workflow["jobs"]["publish"]
    assert acquire["permissions"]=={"contents":"read"}
    assert "inputs.operation == 'stage'" in acquire["if"]
    assert "inputs.operation == 'publish'" in publish["if"]
    for step in acquire["steps"]:
        assert "GH_TOKEN" not in step.get("env",{})
    writer=next(step for step in publish["steps"] if "GH_TOKEN" in step.get("env",{}))
    assert not any("SSH" in key for key in writer["env"])
    assert writer["run"]=="python scripts/publish_match_samples.py"
    assert all("secrets.SERVER" not in str(step) for step in publish["steps"] if step is writer)
    fallback=(ROOT/".github/workflows/protected-match-sample-fallback.yml").read_text()
    assert "samples.cms" in fallback and "upload-artifact@" in fallback
    ssh=(ROOT/"infra/audit/sample_ssh.sh").read_text()
    assert "StrictHostKeyChecking=yes" in ssh and "unset GH_TOKEN GITHUB_TOKEN" in ssh
    subprocess.run(["bash","-n"],input=ssh,text=True,check=True)
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "run" in step: subprocess.run(["bash","-n"],input=step["run"],text=True,check=True)


def test_changed_human_review_copy_is_not_released(sample,tmp_path):
    data,approval,_=sample
    root=tmp_path/"private"
    stage.store(data,TICKET,SHA,root=root)
    record=next((root/TICKET/"review/catalog/samples").glob("match_*/matches.json"))
    record.write_bytes(record.read_bytes()+b" ")
    with pytest.raises(e.ExportError,match="stage_review_changed"):
        stage.release(TICKET,e.sha(data),SHA,approval,root=root)
