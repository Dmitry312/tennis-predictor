"""Fabricated records and mocked GitHub; no production dispatch or publication."""
import io
import json
from pathlib import Path
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import acquire_match_samples as a
from scripts import export_match_samples as e
from scripts import publish_match_samples as p
from scripts import sample_progress as progress
from scripts import sample_workflow_capture as capture
from scripts import sample_workflow_request as request
from scripts import build_sample_acquisition_payload as payload
from scripts.sample_policy import standing_policy
from scripts.sample_delivery import unpack_archive, import_validated
from tests.test_sample_acquisition import CandidatePG,S3,objects,SPORT
from tests.test_sample_publication import API

SHA="a"*40


class AutoAPI(API):
    def __init__(self,pending=None,baseline=None):
        super().__init__()
        self.pending=pending or []
        self.baseline=baseline
    def call(self,method,path,data=None):
        if path.startswith("pulls?"):
            self.calls.append((method,path,data))
            return self.pending
        if path=="pulls/72":
            self.calls.append((method,path,data))
            return self.baseline
        return super().call(method,path,data)


def checkout(root):
    catalog=root/"docs/ml/data_catalog"; catalog.mkdir(parents=True)
    (catalog/"index.json").write_text(json.dumps({"catalog_version":2,"real_examples":[],
        "entries":[{"origin":"synthetic","path":"examples.synthetic.json"}]}))
    return root


def batch(root,plan=None,offset=0,size=2,automatic=True,db=None):
    db=db or CandidatePG()
    plan,report=a.acquire(db,S3(objects(db.rows)),"fixture.bucket",root/"catalog",
        plan=plan,offset=offset,batch_size=size,publication_policy=standing_policy() if automatic else None)
    report["source_commit"]=SHA
    return a.make_archive(root/"catalog",plan,report),plan,report


def apply_tree(api,root):
    tree=next(data for _,path,data in api.calls if path=="git/trees")
    for item in tree["tree"]:
        path=root/item["path"]; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(item["content"],encoding="utf-8")


def test_one_call_publishes_without_attestations_or_anonymization(tmp_path):
    archive,plan,report=batch(tmp_path/"source")
    api=AutoAPI()
    result=p.publish_automatic(api,archive,checkout(tmp_path/"checkout"),SHA,"gh-1-1")
    assert result=={"number":999}
    assert "content_and_source_redistribution_rights_review_required" not in report["blockers"]
    files=next(data for _,path,data in api.calls if path=="git/trees")["tree"]
    details=json.loads(next(item["content"] for item in files if item["path"].endswith("/nbbet_details.json")))
    assert details["versions"][0]["rows"][0]==dict(SPORT,canonical_match_id=details["match_id"])
    receipt=json.loads(next(item["content"] for item in files if "/acquisition-reviews/" in item["path"]))
    assert receipt["publication_policy"]==standing_policy() and "review" not in receipt
    assert receipt["third_party_rights"]=="not_certified_owner_responsibility"
    body=next(data["body"] for _,path,data in api.calls if path=="pulls")
    assert "does not establish third-party redistribution rights" in body
    assert not any(path.endswith("/merge") or method=="PATCH" for method,path,_ in api.calls)


def test_pr72_pending_blocks_before_source_access(tmp_path):
    api=AutoAPI(pending=[{"number":72,"head":{"ref":"samples/first20-gh-45770870231-1"}}])
    with pytest.raises(e.ExportError,match="pending_sample_pr"):
        progress.preflight(api,checkout(tmp_path/"checkout"),SHA)
    assert not any(method=="POST" for method,_,_ in api.calls)


def test_closed_unmerged_baseline_cannot_be_silently_rediscovered(tmp_path):
    api=AutoAPI(baseline={"number":72,"state":"closed","merged":False,
                         "head":{"ref":"samples/first20-gh-45770870231-1"}})
    with pytest.raises(e.ExportError,match="baseline_catalog_missing"):
        progress.preflight(api,checkout(tmp_path/"checkout"),SHA)


def test_resume_from_legacy_two_samples_through_first20_without_duplicates(tmp_path):
    target=checkout(tmp_path/"checkout")
    archive,plan,_=batch(tmp_path/"legacy",automatic=False)
    unpack_archive(archive,tmp_path/"legacy-unpacked")
    import_validated(tmp_path/"legacy-unpacked",target/"docs/ml/data_catalog")
    plan_path=target/"docs/ml/data_catalog/acquisition-plans"/(e.sha(e.dumps(plan))+".json")
    plan_path.parent.mkdir(); plan_path.write_bytes(e.dumps(plan))
    first=target/"docs/ml/data_catalog/samples"/("match_"+plan["selected_ids"][0])/"matches.json"
    original=first.read_bytes()
    offsets=[]
    for i in range(9):
        state=progress.preflight(AutoAPI(),target,SHA)
        offsets.append(state["offset"])
        archive,_,_=batch(tmp_path/("batch"+str(i)),plan=plan,offset=state["offset"])
        api=AutoAPI()
        p.publish_automatic(api,archive,target,SHA,"gh-"+str(i+2)+"-1")
        apply_tree(api,target)  # Simulate the owner's later merge, never a real GitHub merge.
    assert offsets==list(range(2,20,2))
    assert first.read_bytes()==original
    index=e.read_json(target/"docs/ml/data_catalog/index.json",1024*1024)
    assert len(index["real_examples"])==20 and len({row["match_id"] for row in index["real_examples"]})==20
    assert index["entries"]==[{"origin":"synthetic","path":"examples.synthetic.json"}]
    with pytest.raises(e.ExportError,match="first20_complete"):
        progress.preflight(AutoAPI(),target,SHA)


@pytest.mark.parametrize("secret",[{"password":"fixture-password"},{"email":"fixture@example.invalid"},
    {"raw_json":'{"api_key":"fixture-token"}'},{"notes":"-----BEGIN RSA PRIVATE KEY-----"}])
def test_automatic_publisher_still_blocks_sensitive_payloads(tmp_path,monkeypatch,secret):
    db=CandidatePG()
    for row in db.rows.values(): row.update(secret)
    # Forge a malicious but internally checksummed archive; production never disables guards.
    with monkeypatch.context() as patch:
        patch.setattr(e,"inspect_content",lambda *args,**kwargs:None)
        archive,_,_=batch(tmp_path/"source",db=db)
    api=AutoAPI()
    with pytest.raises(ValueError):
        p.publish_automatic(api,archive,checkout(tmp_path/"checkout"),SHA,"gh-1-1")
    assert not api.calls


def test_pending_guard_rechecked_immediately_before_writes(tmp_path):
    archive,_,_=batch(tmp_path/"source")
    api=AutoAPI(pending=[{"head":{"ref":"samples/first20-some-pending"}}])
    with pytest.raises(e.ExportError,match="pending_sample_pr"):
        p.publish_automatic(api,archive,checkout(tmp_path/"checkout"),SHA,"gh-1-1")
    assert not any(method=="POST" for method,_,_ in api.calls)


def test_capture_and_request_have_no_per_batch_review_fields(tmp_path,capsys):
    archive,_,_=batch(tmp_path/"source")
    path=tmp_path/"transport.zip"
    capture.capture(io.BytesIO(archive),{"OPERATION":"automatic","SAMPLE_ARCHIVE":str(path)})
    assert path.read_bytes()==archive and path.stat().st_mode & 0o777==0o600
    assert capsys.readouterr().out==""
    env={"OPERATION":"automatic","REVIEWED_SHA":SHA,"SELECTED_SHA":SHA,"STAGE_TICKET":"gh-1-1",
         "PROJECT_PATH":"/srv/existing","BATCH_SIZE":"2","BATCH_OFFSET":"0"}
    options=request.parameters(env)
    assert options["approval"] is None and options["operation"]=="automatic"
    compile(payload.build(**options),"automatic-reviewed-payload","exec")


def test_default_workflow_is_single_dispatch_without_new_secrets_or_artifacts():
    import yaml
    text=(ROOT/".github/workflows/automatic-match-samples.yml").read_text()
    wf=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(wf["on"])=={"workflow_dispatch"} and len(wf["jobs"])==1
    inputs=wf["on"]["workflow_dispatch"]["inputs"]
    for forbidden in ("content_reviewed","content_evidence","rights_reviewed","rights_evidence"):
        assert forbidden not in inputs and forbidden not in text
    assert len(inputs)<=10
    assert "certificate" not in text.lower() and "upload-artifact" not in text and "cryptography" not in text
    job=wf["jobs"]["samples-to-pr"]
    assert "refs/heads/ML" in job["if"] and job["environment"]=="match-sample-review"
    source=next(v for v in job["steps"] if v.get("env",{}).get("OPERATION")=="automatic")
    assert "GH_TOKEN" not in source["env"] and "GITHUB_TOKEN" not in source["env"]
    writer=next(v for v in job["steps"] if "--automatic" in v.get("run",""))
    assert not any("SSH" in key for key in writer["env"])
    secrets={value for step in job["steps"] for value in step.get("env",{}).values() if "secrets." in value}
    assert len(secrets)==4
    assert "unset GH_TOKEN GITHUB_TOKEN" in (ROOT/"infra/audit/sample_ssh.sh").read_text()
    for step in job["steps"]:
        if "uses" in step: assert len(step["uses"].split("@")[1])==40
        if "run" in step: subprocess.run(["bash","-n"],input=step["run"],text=True,check=True)
