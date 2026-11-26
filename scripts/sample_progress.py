"""Bounded local first-20 resume state and pending-PR checks before source access."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import export_match_samples as e
from scripts import acquire_match_samples as a
from scripts.publish_match_samples import GitHub, pending_samples
from scripts.sample_policy import standing_policy
from infra.audit import sample_diagnostics as diag


def progress(checkout,selection_plan=None,batch_size=2):
    e.need(type(batch_size)is int and 1<=batch_size<=5,"batch_invalid")
    checkout=Path(checkout)
    catalog=checkout/"docs/ml/data_catalog"
    index=e.read_json(catalog/"index.json",1024*1024)
    entries=index.get("real_examples",[])
    e.need(type(entries)is list,"index_invalid")
    ids=[e.mid(entry["match_id"]) for entry in entries]
    e.need(len(ids)==len(set(ids)),"index_invalid")
    if selection_plan:
        import re
        e.need(re.fullmatch(r"docs/ml/data_catalog/acquisition-plans/[a-f0-9]{64}\.json",selection_plan),
               "selection_plan_invalid")
        files=[checkout/selection_plan]
    else:
        directory=catalog/"acquisition-plans"
        e.need(not directory.is_symlink(),"unsafe_catalog_path")
        files=sorted(directory.glob("*.json")) if directory.exists() else []
    e.need(len(files)<=1,"ambiguous_selection_plan")
    if not files:
        e.need(not entries,"selection_plan_required")
        return {"offset":0,"plan_path":"","complete":False}
    file=files[0]
    e.need(not file.is_symlink() and file.resolve().is_relative_to(catalog.resolve()),"unsafe_catalog_path")
    plan=e.read_json(file,1024*1024); a.validate_plan(plan)
    e.need(file.stem==e.sha(e.dumps(plan)),"selection_plan_invalid")
    selected=plan["selected_ids"]
    present=set(ids)
    offset=0
    while offset<len(selected) and selected[offset] in present: offset+=1
    e.need(not any(key in present for key in selected[offset:]),"noncontiguous_catalog")
    if offset:
        # Verify only this bounded first-20 prefix; no arbitrary catalog sweep.
        e.reuse(catalog,selected[:offset])
    return {"offset":offset,"plan_path":str(file.relative_to(checkout)),
            "complete":offset==len(selected),"shortfall":plan["shortfall"]}


def preflight(api,checkout,source_sha,selection_plan=None,batch_size=2,*,allow_complete=False):
    standing_policy()
    e.need(not pending_samples(api),"pending_sample_pr")
    e.need(api.call("GET","git/ref/heads/ML")["object"]["sha"]==source_sha,"ml_changed_before_publication")
    result=progress(checkout,selection_plan,batch_size)
    if result["offset"]==0 and not result["plan_path"]:
        # This repository already published its first two examples in PR #72.
        # Closing that PR without merging must not silently rediscover/duplicate them.
        baseline=api.call("GET","pulls/72")
        if baseline and baseline.get("head",{}).get("ref")=="samples/first20-gh-45770870231-1":
            raise e.ExportError("baseline_catalog_missing")
    if not allow_complete: e.need(not result["complete"],"first20_complete")
    return result


def main():
    try:
        with diag.at("catalog_progress"):
            e.need(os.environ["REVIEWED_SHA"]==os.environ["SELECTED_SHA"],"reviewed_sha")
            api=GitHub(os.environ["GITHUB_REPOSITORY"],os.environ["GH_TOKEN"])
            result=preflight(api,ROOT,os.environ["REVIEWED_SHA"],os.environ.get("SELECTION_PLAN"),
                             int(os.environ.get("BATCH_SIZE","2")),
                             allow_complete=os.environ.get("PLAN_OPERATION")=="true")
            if os.environ.get("PLAN_OPERATION")=="true":
                if result["complete"]:
                    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                        summary.write("Sample plan already complete; no source access or PR required.\n")
                elif result["plan_path"]:
                    from scripts.sample_operation import operation_branch
                    plan=e.read_json(ROOT/result["plan_path"],1024*1024)
                    branch=operation_branch(os.environ["REVIEWED_SHA"],plan,result["offset"])
                    e.need(api.call("GET","git/ref/heads/"+branch) is None,"sample_branch_exists")
            # Only validated scalar metadata is persisted, never token or source rows.
            with Path(os.environ["GITHUB_ENV"]).open("a") as output:
                output.write("BATCH_OFFSET="+str(result["offset"])+"\nPLAN_PATH="+result["plan_path"]+"\n"
                             +"OPERATION_COMPLETE="+str(result["complete"]).lower()+"\n")
        return 0
    except Exception:
        return 1


if __name__=="__main__": raise SystemExit(main())
