"""Build reviewed code payload without credentials; stdout is SSH stdin only."""
import argparse
import base64
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import acquire_match_samples as a
from scripts import export_match_samples as e
from infra.audit.sample_acquisition_bootstrap import FILES


def document(path):
    if not path: return None
    e.need(re.fullmatch(r"docs/ml/data_catalog/[A-Za-z0-9_./-]+\.json",path) is not None
           and ".." not in Path(path).parts,"reviewed_input_path")
    target = ROOT/path
    e.need(not target.is_symlink() and target.resolve().is_relative_to(ROOT/"docs/ml/data_catalog"),
           "reviewed_input_path")
    return e.read_json(target,1024*1024)


def build(project, sha, offset, batch_size, pages, after=None, plan_path=None, manifest_path=None, operation="download",
          stage_ticket=None, approval=None, allow_existing_root=False, plan_data=None):
    e.need(re.fullmatch(r"/[A-Za-z0-9_./-]{1,200}",project) is not None
           and ".." not in Path(project).parts and
           (allow_existing_root or (project!="/root" and not project.startswith("/root/"))),"project_path")
    e.need(re.fullmatch("[a-f0-9]{40}",sha) is not None,"reviewed_sha")
    e.need(0<=offset<20 and 1<=batch_size<=5 and 1<=pages<=4,"batch_invalid")
    e.need(plan_data is None or not plan_path,"selection_ambiguous")
    plan = plan_data if plan_data is not None else document(plan_path)
    if plan is not None: a.validate_plan(plan)
    e.need(offset==0 or plan is not None,"resume_requires_selection_plan")
    after=e.mid(after) if after else None
    e.need(plan is None or after is None,"resume_selection_ambiguous")
    e.need(operation in ("download","stage","release","cleanup","automatic"),"operation_invalid")
    if operation!="download":
        from scripts.review_staging import ticket, review
        ticket(stage_ticket)
        if operation=="release": review(approval)
    if operation=="automatic":
        from scripts.sample_policy import standing_policy
        standing_policy()
    request = {"operation":operation,"ticket":stage_ticket,"approval":approval,
               "allow_existing_root":bool(allow_existing_root),"offset":offset,"batch_size":batch_size,"pages":pages,"after":after,
               "plan":plan,"supplemental":document(manifest_path)}
    e.inspect_content(request)
    package = {"files":{p:(ROOT/p).read_text() for p in FILES},
               "sha":sha,"project":project,"request":request}
    encoded = base64.b64encode(json.dumps(package).encode()).decode()
    bootstrap = (ROOT/"infra/audit/sample_acquisition_bootstrap.py").read_text()
    return bootstrap+"\nraise SystemExit(main("+repr(encoded)+"))\n"


def main():
    try:
        p=argparse.ArgumentParser()
        p.add_argument("--project",required=True); p.add_argument("--sha",required=True)
        p.add_argument("--offset",type=int,default=0); p.add_argument("--batch-size",type=int,default=2)
        p.add_argument("--pages",type=int,default=4); p.add_argument("--after")
        p.add_argument("--plan-path"); p.add_argument("--manifest-path")
        p.add_argument("--allow-existing-root",action="store_true")
        args=p.parse_args()
        sys.stdout.write(build(**vars(args)))
        return 0
    except Exception:
        print("sample_payload_invalid",file=sys.stderr)
        return 1


if __name__=="__main__": raise SystemExit(main())
