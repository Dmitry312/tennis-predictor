"""Reviewed sample PR publication. No production access or acquisition dependencies."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import export_match_samples as e
from scripts.sample_delivery import unpack_archive, import_reviewed, LIMIT
from scripts.review_staging import ticket, review


class GitHub:
    def __init__(self, repository, token):
        e.need(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",repository) and token,"github_config")
        self.base="https://api.github.com/repos/"+repository+"/"
        self.token=token

    def call(self, method, path, data=None):
        request=Request(self.base+path,data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,
                        method=method,headers={"Authorization":"Bearer "+self.token,
                        "Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28",
                        "Content-Type":"application/json"})
        try:
            with urlopen(request,timeout=20) as response:
                raw=response.read(4*1024*1024+1)
            e.need(len(raw)<=4*1024*1024,"github_response_limit")
            return json.loads(raw)
        except HTTPError as exc:
            if method=="GET" and exc.code==404: return None
            raise e.ExportError("sample_pr_publication_failed") from None


def prepare(archive, checkout, source_sha, stage_ticket, approval):
    """Complete all validation and merge locally before any GitHub write."""
    approval=review(approval)
    ticket(stage_ticket)
    e.need(re.fullmatch("[a-f0-9]{40}",source_sha) and e.sha(archive)==approval["archive_sha256"],
           "review_digest_mismatch")
    checkout=Path(checkout)
    with tempfile.TemporaryDirectory(prefix="verified-sample-pr-") as temp:
        temp=Path(temp)
        unpack_archive(archive,temp/"review")
        report=e.read_json(temp/"review"/"acquisition-report.json",1024*1024)
        plan=e.read_json(temp/"review"/"selection.json",1024*1024)
        from scripts.acquire_match_samples import validate_plan
        validate_plan(plan)
        e.need(report.get("source_commit")==source_sha and type(report.get("exported_count"))is int and report["exported_count"] in range(1,6) and
               report.get("plan_sha256")==e.sha(e.dumps(plan)),"publication_source_mismatch")
        # Start from tracked index; never directory-sweep or copy arbitrary checkout files.
        merged=temp/"catalog"; merged.mkdir()
        base=checkout/"docs/ml/data_catalog/index.json"
        if base.exists():
            e.need(not base.is_symlink(),"unsafe_catalog_path")
            (merged/"index.json").write_bytes(base.read_bytes())
        # Existing index entries enforce immutable bundle digest conflicts.
        import_reviewed(temp/"review",merged,content_reviewed=True,rights_reviewed=True)
        loaded=e.reuse(temp/"review"/"catalog")
        e.need(len(loaded)==report["exported_count"],"publication_batch_count")
        for key,bundle in loaded.items():
            e.need(bundle["manifest"]["selection"]["plan_sha256"]==report["plan_sha256"],"publication_plan_mismatch")
        files={"docs/ml/data_catalog/index.json":(merged/"index.json").read_bytes()}
        for key in loaded:
            for name in (*e.FILES,"manifest.json"):
                path="samples/match_"+key+"/"+name
                files["docs/ml/data_catalog/"+path]=(merged/path).read_bytes()
        files["docs/ml/data_catalog/acquisition-plans/"+report["plan_sha256"]+".json"]=e.dumps(plan)
        receipt={"stage_ticket":stage_ticket,"source_commit":source_sha,"review":approval,
                 "report":report,"publication_scope":"ordinary_sports_data_owner_approved_2026-11-25"}
        files["docs/ml/data_catalog/acquisition-reviews/"+stage_ticket+".json"]=e.dumps(receipt)
    e.need(sum(map(len,files.values()))<=32*1024*1024,"publication_output_limit")
    for value in files.values():
        # All generated files are JSON. Scan again with the publication token excluded.
        content=json.loads(value,object_pairs_hook=e.unique)
        e.inspect_content(content,(os.environ.get("GH_TOKEN",""),))
    return files


def publish(api, archive, checkout, source_sha, stage_ticket, approval):
    files=prepare(archive,checkout,source_sha,stage_ticket,approval)
    # Review and content validation must precede even creation of Git objects.
    review(approval); ticket(stage_ticket)
    e.need(re.fullmatch("[a-f0-9]{40}",source_sha) and type(files)is dict and files,"publication_invalid")
    for path,data in files.items():
        e.need(path.startswith("docs/ml/data_catalog/") and ".." not in Path(path).parts and
               path.endswith(".json"),"publication_path")
        e.inspect_content(json.loads(data,object_pairs_hook=e.unique),(getattr(api,"token",""),))
    branch="samples/first20-"+stage_ticket
    current=api.call("GET","git/ref/heads/ML")
    e.need(current["object"]["sha"]==source_sha,"ml_changed_before_publication")
    e.need(api.call("GET","git/ref/heads/"+branch) is None,"sample_branch_exists")
    commit=api.call("GET","git/commits/"+source_sha)
    tree=api.call("POST","git/trees",{"base_tree":commit["tree"]["sha"],"tree":[
        {"path":p,"mode":"100644","type":"blob","content":data.decode("utf-8")} for p,data in sorted(files.items())]})
    created=api.call("POST","git/commits",{"message":"Add reviewed sports sample batch "+stage_ticket,
        "tree":tree["sha"],"parents":[source_sha]})
    # Never move ML; never force-push an existing branch.
    e.need(api.call("GET","git/ref/heads/ML")["object"]["sha"]==source_sha,"ml_changed_before_publication")
    api.call("POST","git/refs",{"ref":"refs/heads/"+branch,"sha":created["sha"]})
    return api.call("POST","pulls",{"base":"ML","head":branch,
        "title":"ML: reviewed real sports sample batch "+stage_ticket,
        "body":"Reviewed staged sports JSON; no merge performed.\n\nArchive SHA-256: "+
            approval["archive_sha256"]+"\nContent review: "+approval["content_evidence"]+
            "\nSource redistribution rights review: "+approval["rights_evidence"]+
            "\n\nOriginal identities and values retained; missing coverage is recorded in the catalog."})


def main():
    try:
        archive_path=Path(os.environ["SAMPLE_ARCHIVE"])
        with archive_path.open("rb") as stream: data=stream.read(LIMIT+1)
        e.need(len(data)<=LIMIT,"publication_input_limit")
        approval=e.read_json(os.environ["REVIEW_FILE"],4096)
        api=GitHub(os.environ["GITHUB_REPOSITORY"],os.environ["GH_TOKEN"])
        result=publish(api,data,ROOT,os.environ["REVIEWED_SHA"],os.environ["STAGE_TICKET"],approval)
        e.need(type(result.get("number"))is int,"publication_response")
        print("sample_pr_created:"+str(result["number"]))
        return 0
    except Exception:
        print("sample_pr_publication_failed",file=sys.stderr)
        return 1


if __name__=="__main__": raise SystemExit(main())
