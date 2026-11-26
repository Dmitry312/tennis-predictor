"""One bounded logical plan operation; sequential reads, one all-or-nothing PR."""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import export_match_samples as e
from scripts import acquire_match_samples as a
from scripts import publish_match_samples as p
from scripts.sample_policy import standing_policy
from scripts.sample_delivery import LIMIT
from infra.audit import sample_diagnostics as diag

MAX_BATCHES=4
MAX_MATCHES=20
MAX_ARCHIVES=256*1024*1024
MAX_OUTPUT=128*1024*1024
MAX_SECONDS=1200
MAX_STORAGE=512*1024*1024
MEMORY=2*1024**3
USAGE_CAPS={"s3_calls":200,"s3_bytes":128*1024*1024,"decoded_rows":300000,"http_attempts":200}
FORMAT="sample-plan-operation-v1"


@contextmanager
def bounded_runner(seconds):
    previous=resource.getrlimit(resource.RLIMIT_AS)
    old_alarm=signal.alarm(0)
    old=signal.getsignal(signal.SIGALRM)
    old_term=signal.getsignal(signal.SIGTERM)
    cap=min(previous[0],MEMORY) if previous[0]!=resource.RLIM_INFINITY else MEMORY
    def expired(*_): raise e.ExportError("deadline")
    started=time.monotonic()
    try:
        resource.setrlimit(resource.RLIMIT_AS,(cap,previous[1]))
        signal.signal(signal.SIGALRM,expired)
        signal.signal(signal.SIGTERM,expired)
        signal.alarm(min(seconds,old_alarm) if old_alarm else seconds)
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM,old)
        signal.signal(signal.SIGTERM,old_term)
        resource.setrlimit(resource.RLIMIT_AS,previous)
        if old_alarm: signal.alarm(max(1,old_alarm-int(time.monotonic()-started)))


def limits():
    return {"batches":MAX_BATCHES,"seconds":MAX_SECONDS,"archive_bytes":MAX_ARCHIVES,
            "output_bytes":MAX_OUTPUT,"private_storage_bytes":MAX_STORAGE,"memory_bytes":MEMORY}


def operation_id(source_sha,plan,offset):
    return e.sha(e.dumps({"source_sha":source_sha,"plan_sha256":e.sha(e.dumps(plan)),"offset":offset}))


def operation_branch(source_sha,plan,offset):
    return "samples/first20-plan-"+operation_id(source_sha,plan,offset)[:32]


def write_private(path,data):
    with Path(path).open("xb") as stream:
        os.chmod(path,0o600)
        stream.write(data)


def local_plan(checkout,path):
    if not path: return None
    e.need(re.fullmatch(r"docs/ml/data_catalog/acquisition-plans/[a-f0-9]{64}\.json",path),
           "selection_plan_invalid")
    file=Path(checkout)/path
    e.need(not file.is_symlink() and file.resolve().is_relative_to(Path(checkout).resolve()/"docs/ml/data_catalog"),
           "unsafe_catalog_path")
    plan=e.read_json(file,1024*1024); a.validate_plan(plan)
    e.need(file.stem==e.sha(e.dumps(plan)),"selection_plan_invalid")
    return plan


def usage(report):
    value=report.get("read_usage")
    e.need(type(value)is dict and set(value)==set(USAGE_CAPS),"operation_usage_invalid")
    e.need(all(type(value[k])is int and 0<=value[k]<=cap for k,cap in USAGE_CAPS.items()),
           "operation_limit")
    e.need(value["http_attempts"]<=value["s3_calls"],"operation_usage_invalid")
    return value


def checked_batch(data,checkout,source_sha,ticket,plan,offset,count):
    files=p.prepare_validated(data,checkout,source_sha,ticket,{
        "publication_policy":standing_policy(),"automatic_validation":"integrity_and_secret_contact_checks_passed",
        "third_party_rights":"not_certified_owner_responsibility"},automatic=True)
    receipt_path="docs/ml/data_catalog/acquisition-reviews/"+ticket+".json"
    receipt=json.loads(files[receipt_path],object_pairs_hook=e.unique)
    report=receipt["report"]
    incoming_plan=json.loads(next(value for path,value in files.items() if "/acquisition-plans/" in path),
                             object_pairs_hook=e.unique)
    a.validate_plan(incoming_plan)
    if plan is None: plan=incoming_plan
    e.need(e.dumps(incoming_plan)==e.dumps(plan),"operation_plan_changed")
    expected=plan["selected_ids"][offset:offset+count]
    e.need(expected and type(report.get("batch_offset"))is int and report["batch_offset"]==offset and
           report["exported_count"]==len(expected) and report.get("next_offset")==offset+len(expected),
           "operation_slice_invalid")
    actual=[]
    for path,value in files.items():
        if "/samples/" in path and path.endswith("/manifest.json"):
            manifest=json.loads(value,object_pairs_hook=e.unique)
            selection=manifest["selection"]
            e.need(selection["batch_offset"]==offset and selection["batch_size"]==len(expected),
                   "operation_slice_invalid")
            actual.append(manifest["match_id"])
    e.need(len(actual)==len(expected) and set(actual)==set(expected),"operation_slice_invalid")
    return files,plan,report,usage(report)


def ssh_fetch(offset,count,plan,target,*,checkout,work,end):
    env=dict(os.environ,OPERATION="automatic",BATCH_OFFSET=str(offset),BATCH_SIZE=str(count),
             SAMPLE_ARCHIVE=str(target),PLAN_PATH="")
    env.pop("GH_TOKEN",None); env.pop("GITHUB_TOKEN",None)
    env.pop("OPERATION_PLAN_FILE",None)
    if plan is not None:
        path=work/"inline-plan.json"
        if path.exists(): path.unlink()
        write_private(path,e.dumps(plan))
        env["OPERATION_PLAN_FILE"]=str(path)
    seconds=min(300,max(1,int(end-time.monotonic())))
    process=subprocess.Popen(["bash",str(Path(checkout)/"infra/audit/sample_ssh.sh")],
        cwd=checkout,env=env,stdout=subprocess.DEVNULL,start_new_session=True)
    try:
        e.need(process.wait(timeout=seconds)==0,"operation_batch_failed")
    except BaseException:
        # Kill the whole local SSH/relay/capture process group, not just its shell.
        try: os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()
        raise


def collect(checkout,work,source_sha,ticket,*,offset,plan_path="",batch_size=5,fetch=None):
    checkout=Path(checkout); work=Path(work)
    e.need(re.fullmatch("[a-f0-9]{40}",source_sha) and type(offset)is int and 0<=offset<=20 and
           type(batch_size)is int and 1<=batch_size<=5,"operation_invalid")
    from scripts.review_staging import ticket as valid_ticket
    valid_ticket(ticket)
    plan=local_plan(checkout,plan_path)
    if plan is not None:
        e.need(offset<=len(plan["selected_ids"]),"operation_slice_invalid")
        if offset==len(plan["selected_ids"]): return {"noop":True}
        e.need(math.ceil((len(plan["selected_ids"])-offset)/batch_size)<=MAX_BATCHES,"operation_limit")
    else: e.need(offset==0,"selection_plan_required")
    e.need(not work.exists() and not work.is_symlink(),"operation_workspace")
    work.mkdir(mode=0o700,parents=True)
    end=time.monotonic()+MAX_SECONDS
    archive_total=0
    totals={key:0 for key in USAGE_CAPS}
    batches=[]
    start=offset
    try:
        baseline=checkout/"docs/ml/data_catalog/index.json"
        baseline_hash=e.sha(baseline.read_bytes())
        while plan is None or offset<len(plan["selected_ids"]):
            e.need(time.monotonic()<end and len(batches)<MAX_BATCHES,"operation_limit")
            count=min(batch_size,len(plan["selected_ids"])-offset) if plan is not None else batch_size
            target=work/("batch-"+str(len(batches)).zfill(3)+".zip")
            with diag.at("operation_batch"):
                if fetch is None: ssh_fetch(offset,count,plan,target,checkout=checkout,work=work,end=end)
                else: fetch(offset,count,plan,target)
                e.need(target.is_file() and not target.is_symlink() and 0<target.stat().st_size<=LIMIT,
                       "operation_archive_limit")
                archive_total+=target.stat().st_size
                e.need(archive_total<=MAX_ARCHIVES,"operation_limit")
                with target.open("rb") as stream: data=stream.read(LIMIT+1)
                files,plan,report,used=checked_batch(data,checkout,source_sha,ticket,plan,offset,count)
                e.need(math.ceil((len(plan["selected_ids"])-start)/batch_size)<=MAX_BATCHES,"operation_limit")
                for key in totals:
                    totals[key]+=used[key]
                    e.need(totals[key]<=MAX_BATCHES*USAGE_CAPS[key],"operation_limit")
                # Hash each exact validated JSON result for the later fresh publisher process.
                checksums={path:e.sha(value) for path,value in files.items()}
                batches.append({"file":target.name,"sha256":e.sha(data),"offset":offset,
                                "count":report["exported_count"],"json_sha256":checksums,"read_usage":used})
                offset+=report["exported_count"]
                del files,data
            e.need(time.monotonic()<end,"deadline")
        e.need(offset<=MAX_MATCHES and offset==len(plan["selected_ids"]),"operation_incomplete")
        manifest={"format":FORMAT,"source_sha":source_sha,"ticket":ticket,"plan":plan,
                  "start_offset":start,"end_offset":offset,"batches":batches,"archive_bytes":archive_total,
                  "read_usage":totals,"baseline_index_sha256":baseline_hash,
                  "operation_id":operation_id(source_sha,plan,start),"complete":True,
                  "limits":limits()}
        e.inspect_content(manifest)
        encoded=e.dumps(manifest)
        e.need(len(encoded)<=1024*1024,"operation_limit")
        write_private(work/"operation.json",encoded)  # Written only after all batches succeed.
        return manifest
    except BaseException:
        shutil.rmtree(work,ignore_errors=True)
        raise


def prepare_operation(work,checkout,source_sha,ticket):
    work=Path(work); checkout=Path(checkout)
    from scripts.review_staging import owned
    owned(work)
    owned(work/"operation.json",False)
    state=e.read_json(work/"operation.json",1024*1024)
    e.need(state.get("format")==FORMAT and state.get("complete")is True and
           state.get("source_sha")==source_sha and state.get("ticket")==ticket and
           state.get("limits")==limits(),"operation_incomplete")
    plan=state["plan"]; a.validate_plan(plan)
    start=state["start_offset"]
    e.need(type(start)is int and 0<=start<len(plan["selected_ids"]) and len(plan["selected_ids"])<=20 and
           state["operation_id"]==operation_id(source_sha,plan,start),"operation_invalid")
    base_path=checkout/"docs/ml/data_catalog/index.json"
    e.need(e.sha(base_path.read_bytes())==state["baseline_index_sha256"],"operation_baseline_changed")
    index=e.read_json(base_path,1024*1024)
    entries={entry["match_id"]:entry for entry in index.get("real_examples",[])}
    e.need(len(entries)==len(index.get("real_examples",[])),"index_invalid")
    all_files={}
    next_offset=start
    archive_total=0
    totals={key:0 for key in USAGE_CAPS}
    aggregate=[]
    e.need(type(state["batches"])is list and 1<=len(state["batches"])<=MAX_BATCHES,"operation_limit")
    for i,info in enumerate(state["batches"]):
        name="batch-"+str(i).zfill(3)+".zip"
        e.need(info["file"]==name and info["offset"]==next_offset and
               type(info["count"])is int and 1<=info["count"]<=5,"operation_slice_invalid")
        file=work/name
        owned(file,False)
        e.need(not file.is_symlink() and file.stat().st_size<=LIMIT,"operation_archive_limit")
        with file.open("rb") as stream: data=stream.read(LIMIT+1)
        archive_total+=len(data)
        e.need(archive_total<=MAX_ARCHIVES and e.sha(data)==info["sha256"],"operation_archive_changed")
        files,_,report,used=checked_batch(data,checkout,source_sha,ticket,plan,next_offset,info["count"])
        e.need({path:e.sha(value) for path,value in files.items()}==info["json_sha256"] and
               used==info["read_usage"],"operation_archive_changed")
        for key in totals:
            totals[key]+=used[key]
            e.need(totals[key]<=MAX_BATCHES*USAGE_CAPS[key],"operation_limit")
        batch_index=json.loads(files.pop("docs/ml/data_catalog/index.json"),object_pairs_hook=e.unique)
        selected=plan["selected_ids"][next_offset:next_offset+info["count"]]
        for entry in batch_index["real_examples"]:
            if entry["match_id"] in selected:
                e.need(entry["match_id"] not in entries,"sample_already_catalogued")
                entries[entry["match_id"]]=entry
        receipt_path="docs/ml/data_catalog/acquisition-reviews/"+ticket+".json"
        receipt=json.loads(files.pop(receipt_path),object_pairs_hook=e.unique)
        receipt["operation_id"]=state["operation_id"]; receipt["batch_index"]=i
        batch_path=receipt_path.removesuffix(".json")+"-batch-"+str(i).zfill(3)+".json"
        files[batch_path]=e.dumps(receipt)
        for path,value in files.items():
            e.need(path not in all_files or all_files[path]==value,"operation_duplicate_file")
            all_files[path]=value
        aggregate.append({"offset":next_offset,"count":info["count"],"archive_sha256":info["sha256"],
                          "receipt":batch_path,"read_usage":used})
        next_offset+=info["count"]
        e.need(sum(map(len,all_files.values()))<=MAX_OUTPUT,"operation_limit")
        del files,data
    e.need(next_offset==len(plan["selected_ids"])==state["end_offset"] and
           archive_total==state["archive_bytes"] and totals==state["read_usage"],"operation_incomplete")
    index["real_examples"]=[entries[key] for key in sorted(entries)]
    index["publication_policy"]=standing_policy()
    index["publication_status"]="standing_owner_policy_automatic_validation"
    all_files["docs/ml/data_catalog/index.json"]=e.dumps(index)
    receipt={"format":FORMAT,"operation_id":state["operation_id"],"source_commit":source_sha,
             "plan_sha256":e.sha(e.dumps(plan)),"start_offset":start,"end_offset":next_offset,
             "exported_count":next_offset-start,"internal_batches":aggregate,"complete":True,
             "target":20,"plan_selected_count":len(plan["selected_ids"]),"selection_shortfall":plan["shortfall"],
             "coverage":"all_requested_ids_represented_not_a_complete_history_claim",
             "read_usage":totals,"archive_bytes":archive_total,"limits":state["limits"],
             "publication_policy":standing_policy(),"third_party_rights":"not_certified_owner_responsibility"}
    all_files["docs/ml/data_catalog/acquisition-reviews/"+ticket+".json"]=e.dumps(receipt)
    e.need(sum(map(len,all_files.values()))<=MAX_OUTPUT,"operation_limit")
    for value in all_files.values():
        e.inspect_content(json.loads(value,object_pairs_hook=e.unique),(os.environ.get("GH_TOKEN",""),))
    return all_files,receipt,operation_branch(source_sha,plan,start)


def publish_operation(api,work,checkout,source_sha,ticket):
    with diag.at("publication_validation"):
        files,receipt,branch=prepare_operation(work,checkout,source_sha,ticket)
    description=("Complete remaining sample plan: "+str(receipt["exported_count"])+" matches in "+
        str(len(receipt["internal_batches"]))+" sequential internal batches. All batches validated before "+
        "any GitHub content write. One final result; no automatic merge.\n\nOperation SHA-256: "+
        receipt["operation_id"]+"\nPer-batch provenance, checksums and missing coverage remain in the catalog. "+
        "Owner permission does not certify third-party redistribution rights.")
    with diag.at("github_publication"):
        return p.publish_prepared(api,files,source_sha,ticket,description,automatic=True,branch=branch)


def main():
    try:
        work=Path(os.environ["OPERATION_DIR"])
        with bounded_runner(MAX_SECONDS),diag.at("plan_operation"):
            result=collect(ROOT,work,os.environ["REVIEWED_SHA"],os.environ["STAGE_TICKET"],
                           offset=int(os.environ["BATCH_OFFSET"]),plan_path=os.environ.get("PLAN_PATH",""),
                           batch_size=int(os.environ.get("BATCH_SIZE","5")))
        print("sample_operation_complete:"+str(result.get("end_offset",0)))
        return 0
    except Exception:
        return 1


if __name__=="__main__": raise SystemExit(main())
