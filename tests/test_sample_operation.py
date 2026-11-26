"""One logical operation, fake sources and mocked GitHub only."""
import json
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import sample_operation as op
from scripts import sample_progress as progress
from scripts import acquire_match_samples as a
from scripts import export_match_samples as e
from scripts.sample_delivery import unpack_archive,import_validated
from tests.test_automatic_samples import AutoAPI,checkout,batch,apply_tree,SHA
from tests.test_sample_acquisition import CandidatePG

TICKET="gh-8100-1"


def seeded(root,prefix):
    target=checkout(root/"checkout")
    plan=a.choose(*a.candidate_pool(CandidatePG()))
    for offset in range(0,prefix,5):
        data,_,_=batch(root/("seed"+str(offset)),plan=plan,offset=offset,size=min(5,prefix-offset))
        path=root/("unpack"+str(offset))
        unpack_archive(data,path)
        import_validated(path,target/"docs/ml/data_catalog")
    file=target/"docs/ml/data_catalog/acquisition-plans"/(e.sha(e.dumps(plan))+".json")
    file.parent.mkdir(parents=True,exist_ok=True); file.write_bytes(e.dumps(plan))
    return target,plan,str(file.relative_to(target))


class Fetch:
    def __init__(self,root,fail=None,corrupt=None):
        self.root=root; self.fail=fail; self.corrupt=corrupt; self.calls=[]; self.active=False
    def __call__(self,offset,count,plan,target):
        assert not self.active, "production batches must never overlap"
        self.active=True
        try:
            self.calls.append((offset,count,e.sha(e.dumps(plan)) if plan else None))
            if self.fail==len(self.calls): raise RuntimeError("PRIVATE_SOURCE_FAILURE")
            data,_,_=batch(self.root/("source"+str(len(self.calls))),plan=plan,offset=offset,size=count)
            if self.corrupt==len(self.calls): data+=b"tampered"
            op.write_private(target,data)
        finally: self.active=False


@pytest.mark.parametrize("prefix,sizes",[(12,[5,3]),(0,[5,5,5,5]),(17,[3])])
def test_whole_remaining_plan_one_pr_preserves_committed_prefix(tmp_path,prefix,sizes):
    target,plan,path=seeded(tmp_path,prefix)
    before={file.relative_to(target):file.read_bytes() for file in
            (target/"docs/ml/data_catalog/samples").rglob("*.json")} if prefix else {}
    api=AutoAPI()
    state=progress.preflight(api,target,SHA,batch_size=5,allow_complete=True)
    assert state["offset"]==prefix
    source=Fetch(tmp_path)
    work=tmp_path/"operation"
    manifest=op.collect(target,work,SHA,TICKET,offset=state["offset"],plan_path=path,fetch=source)
    assert [n for _,n,_ in source.calls]==sizes
    assert len({p for _,_,p in source.calls})==1
    assert manifest["complete"] is True and manifest["end_offset"]==20
    assert not any(method=="POST" for method,_,_ in api.calls)
    result=op.publish_operation(api,work,target,SHA,TICKET)
    assert result=={"number":999}
    assert sum(path=="git/refs" for _,path,_ in api.calls)==1
    assert sum(path=="pulls" for _,path,_ in api.calls)==1
    tree=next(value for _,path,value in api.calls if path=="git/trees")
    new_manifests=[item for item in tree["tree"] if "/samples/" in item["path"] and item["path"].endswith("/manifest.json")]
    assert len(new_manifests)==20-prefix
    assert {json.loads(item["content"])["match_id"] for item in new_manifests}==set(plan["selected_ids"][prefix:])
    apply_tree(api,target)
    assert all((target/file).read_bytes()==data for file,data in before.items())
    index=e.read_json(target/"docs/ml/data_catalog/index.json",1024*1024)
    assert len(index["real_examples"])==20
    assert len({v["match_id"] for v in index["real_examples"]})==20
    aggregate=e.read_json(target/("docs/ml/data_catalog/acquisition-reviews/"+TICKET+".json"),1024*1024)
    assert aggregate["exported_count"]==20-prefix
    assert [v["count"] for v in aggregate["internal_batches"]]==sizes
    assert aggregate["complete"] is True and aggregate["coverage"].endswith("not_a_complete_history_claim")
    assert all(v["read_usage"]["s3_calls"]<=200 for v in aggregate["internal_batches"])


def test_second_batch_failure_publishes_nothing_and_safe_restart(tmp_path,capsys):
    target,_,path=seeded(tmp_path,12)
    work=tmp_path/"operation"
    failing=Fetch(tmp_path/"failed",fail=2)
    api=AutoAPI()
    with pytest.raises(RuntimeError):
        op.collect(target,work,SHA,TICKET,offset=12,plan_path=path,fetch=failing)
    assert [n for _,n,_ in failing.calls]==[5,3] and not work.exists()
    with pytest.raises((ValueError,FileNotFoundError)):
        op.publish_operation(api,work,target,SHA,TICKET)
    assert not api.calls
    assert progress.progress(target)["offset"]==12
    retry=Fetch(tmp_path/"retry")
    op.collect(target,work,SHA,TICKET,offset=12,plan_path=path,fetch=retry)
    op.publish_operation(api,work,target,SHA,TICKET)
    assert [n for _,n,_ in retry.calls]==[5,3]
    assert "PRIVATE_SOURCE_FAILURE" not in capsys.readouterr().err


@pytest.mark.parametrize("mode",["stale","pending"])
def test_changed_ml_or_pending_pr_blocks_all_publication(tmp_path,mode):
    target,_,path=seeded(tmp_path,12)
    work=tmp_path/"operation"
    op.collect(target,work,SHA,TICKET,offset=12,plan_path=path,fetch=Fetch(tmp_path))
    class Changed(AutoAPI):
        def call(self,method,path,data=None):
            if mode=="stale" and path=="git/ref/heads/ML": return {"object":{"sha":"b"*40}}
            return super().call(method,path,data)
    api=Changed(pending=[{"head":{"ref":"samples/first20-pending"}}] if mode=="pending" else None)
    with pytest.raises(ValueError): op.publish_operation(api,work,target,SHA,TICKET)
    assert not any(method=="POST" for method,_,_ in api.calls)


@pytest.mark.parametrize("cap",["MAX_BATCHES","MAX_ARCHIVES"])
def test_operation_caps_fail_before_publication(tmp_path,monkeypatch,cap):
    target,_,path=seeded(tmp_path,12)
    monkeypatch.setattr(op,cap,1)
    work=tmp_path/"operation"
    with pytest.raises(ValueError):
        op.collect(target,work,SHA,TICKET,offset=12,plan_path=path,fetch=Fetch(tmp_path))
    assert not work.exists()


def test_output_limit_and_modified_archive_block_writes(tmp_path,monkeypatch):
    target,_,path=seeded(tmp_path,12)
    work=tmp_path/"operation"
    op.collect(target,work,SHA,TICKET,offset=12,plan_path=path,fetch=Fetch(tmp_path))
    api=AutoAPI()
    with monkeypatch.context() as patch:
        patch.setattr(op,"MAX_OUTPUT",1)
        with pytest.raises(ValueError): op.publish_operation(api,work,target,SHA,TICKET)
    assert not api.calls
    file=work/"batch-001.zip"
    file.write_bytes(file.read_bytes()+b"changed")
    with pytest.raises(ValueError,match="operation_archive_changed"):
        op.publish_operation(api,work,target,SHA,TICKET)
    assert not api.calls


def test_wrong_slice_or_plan_cannot_produce_operation_manifest(tmp_path):
    target,plan,path=seeded(tmp_path,12)
    def wrong(offset,count,frozen,destination):
        data,_,_=batch(tmp_path/"wrong",plan=plan,offset=0,size=5)
        op.write_private(destination,data)
    with pytest.raises(ValueError):
        op.collect(target,tmp_path/"operation",SHA,TICKET,offset=12,plan_path=path,fetch=wrong)
    assert not (tmp_path/"operation").exists()


def test_complete_plan_is_no_source_access_noop(tmp_path):
    target,_,path=seeded(tmp_path,20)
    api=AutoAPI()
    state=progress.preflight(api,target,SHA,allow_complete=True)
    assert state["complete"] is True
    def forbidden(*args): pytest.fail("complete plan accessed production sources")
    assert op.collect(target,tmp_path/"operation",SHA,TICKET,offset=20,plan_path=path,fetch=forbidden)=={"noop":True}
    assert not any(method=="POST" for method,_,_ in api.calls)


def test_stable_operation_branch_refuses_retry_without_overwrite(tmp_path):
    target,plan,path=seeded(tmp_path,12)
    work=tmp_path/"operation"
    op.collect(target,work,SHA,TICKET,offset=12,plan_path=path,fetch=Fetch(tmp_path))
    class Existing(AutoAPI):
        def call(self,method,path,data=None):
            if path=="git/ref/heads/"+op.operation_branch(SHA,plan,12): return {"object":{"sha":"existing"}}
            return super().call(method,path,data)
    api=Existing()
    with pytest.raises(ValueError,match="sample_branch_exists"):
        op.publish_operation(api,work,target,SHA,TICKET)
    assert not any(method=="POST" for method,_,_ in api.calls)


def test_workflow_keeps_one_write_step_and_noop_cleanup_guards():
    import yaml
    wf=yaml.load((ROOT/".github/workflows/automatic-match-samples.yml").read_text(),Loader=yaml.BaseLoader)
    job=wf["jobs"]["samples-to-pr"]
    assert job["timeout-minutes"]=="25" and job["environment"]=="match-sample-review"
    source=next(v for v in job["steps"] if v.get("env",{}).get("OPERATION")=="automatic")
    writer=next(v for v in job["steps"] if "--operation" in v.get("run",""))
    assert source["run"]=="python -I -S scripts/sample_operation.py"
    assert "GH_TOKEN" not in source["env"] and not any("SSH" in k for k in writer["env"])
    assert "OPERATION_COMPLETE" in source["if"] and "success()" in writer["if"]
    cleanup=job["steps"][-1]
    assert "always()" in cleanup["if"] and "$OPERATION_DIR" in cleanup["run"]
    assert len(wf["on"]["workflow_dispatch"]["inputs"])==2


def test_publisher_operation_cli_needs_no_single_archive_env(tmp_path,monkeypatch,capsys):
    from contextlib import nullcontext
    from scripts import publish_match_samples as publisher
    target,_,path=seeded(tmp_path,12)
    work=tmp_path/"operation"
    op.collect(target,work,SHA,TICKET,offset=12,plan_path=path,fetch=Fetch(tmp_path))
    api=AutoAPI()
    monkeypatch.setattr(publisher,"ROOT",target)
    monkeypatch.setattr(publisher,"GitHub",lambda *args:api)
    monkeypatch.setattr(op,"bounded_runner",lambda *_:nullcontext())
    monkeypatch.setattr(sys,"argv",["publish_match_samples.py","--operation"])
    monkeypatch.delenv("SAMPLE_ARCHIVE",raising=False)
    for key,value in {"OPERATION_DIR":str(work),"REVIEWED_SHA":SHA,"STAGE_TICKET":TICKET,
                      "GITHUB_REPOSITORY":"fixture/repository","GH_TOKEN":"fixture-token"}.items():
        monkeypatch.setenv(key,value)
    assert publisher.main()==0
    assert "sample_pr_created:999" in capsys.readouterr().out
    assert sum(path=="pulls" for _,path,_ in api.calls)==1


def test_internal_frozen_plan_request_is_validated_without_new_user_input(tmp_path,monkeypatch,capsys):
    import ast,base64
    from scripts import sample_workflow_request as request
    _,plan,_=seeded(tmp_path,0)
    path=tmp_path/"private-plan.json"; op.write_private(path,e.dumps(plan))
    env={"OPERATION":"automatic","REVIEWED_SHA":SHA,"SELECTED_SHA":SHA,"STAGE_TICKET":TICKET,
         "PROJECT_PATH":"/srv/existing","ALLOW_EXISTING_ROOT":"false","BATCH_OFFSET":"12","BATCH_SIZE":"5",
         "OPERATION_PLAN_FILE":str(path),"PLAN_PATH":""}
    for key,value in env.items(): monkeypatch.setenv(key,value)
    assert request.main()==0
    code=capsys.readouterr().out
    tree=ast.parse(code)
    encoded=tree.body[-1].exc.args[0].args[0].value
    package=json.loads(base64.b64decode(encoded))
    assert package["request"]["plan"]==plan and package["request"]["offset"]==12
    monkeypatch.setenv("OPERATION","stage")
    assert request.main()==1
    assert capsys.readouterr().out==""


def test_ssh_child_environment_and_process_group_timeout_cleanup(tmp_path,monkeypatch):
    import subprocess
    _,plan,_=seeded(tmp_path,0)
    work=tmp_path/"work"; work.mkdir(mode=0o700)
    seen={}; killed=[]
    class Process:
        pid=123456789
        def __init__(self,*args,**kwargs):
            seen.update(kwargs); self.attempts=0
        def wait(self,timeout=None):
            self.attempts+=1
            if self.attempts==1: raise subprocess.TimeoutExpired("fixture",timeout)
            return 0
    monkeypatch.setattr(op.subprocess,"Popen",Process)
    monkeypatch.setattr(op.os,"killpg",lambda pid,sig:killed.append((pid,sig)))
    monkeypatch.setenv("GH_TOKEN","PRIVATE_FIXTURE_WRITE_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN","PRIVATE_FIXTURE_WRITE_TOKEN")
    with pytest.raises(subprocess.TimeoutExpired):
        op.ssh_fetch(12,5,plan,work/"batch.zip",checkout=ROOT,work=work,end=op.time.monotonic()+30)
    assert "GH_TOKEN" not in seen["env"] and "GITHUB_TOKEN" not in seen["env"]
    assert seen["start_new_session"] is True and killed==[(Process.pid,op.signal.SIGTERM)]
    assert e.read_json(seen["env"]["OPERATION_PLAN_FILE"],1024*1024)==plan
