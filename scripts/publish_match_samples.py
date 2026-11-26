"""Validated catalog publication; standing owner policy is not a source licence."""
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
from scripts.sample_delivery import unpack_archive, import_validated, LIMIT
from scripts.review_staging import ticket, review
from scripts.sample_policy import standing_policy
from infra.audit import sample_diagnostics as diag

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



def pending_samples(api):
    values=api.call("GET","pulls?state=open&base=ML&per_page=100")
    e.need(type(values)is list and len(values)<100,"sample_pr_inventory_limit")
    return [value for value in values if value.get("head",{}).get("ref","").startswith("samples/first20-")]


def prepare_validated(archive,checkout,source_sha,stage_ticket,context,*,automatic=False):
    """All JSON/integrity/content checks finish locally before a GitHub write."""
    ticket(stage_ticket)
    e.need(re.fullmatch("[a-f0-9]{40}",source_sha) and len(archive)<=LIMIT,"publication_invalid")
    checkout=Path(checkout)
    with tempfile.TemporaryDirectory(prefix="verified-sample-pr-") as temp:
        temp=Path(temp)
        unpack_archive(archive,temp/"review")
        report=e.read_json(temp/"review"/"acquisition-report.json",1024*1024)
        plan=e.read_json(temp/"review"/"selection.json",1024*1024)
        from scripts.acquire_match_samples import validate_plan
        validate_plan(plan)
        e.need(report.get("source_commit")==source_sha and type(report.get("exported_count"))is int and
               report["exported_count"] in range(1,6) and
               report.get("plan_sha256")==e.sha(e.dumps(plan)),"publication_source_mismatch")
        merged=temp/"catalog"; merged.mkdir()
        base=checkout/"docs/ml/data_catalog/index.json"
        baseline={"real_examples":[]}
        if base.exists():
            e.need(not base.is_symlink(),"unsafe_catalog_path")
            baseline=e.read_json(base,1024*1024)
            (merged/"index.json").write_bytes(base.read_bytes())
        loaded=e.reuse(temp/"review"/"catalog")
        e.need(len(loaded)==report["exported_count"],"publication_batch_count")
        if automatic:
            policy=standing_policy()
            e.need(report.get("publication_policy")==policy,"publication_policy_invalid")
            existing={entry["match_id"] for entry in baseline.get("real_examples",[])}
            e.need(not existing.intersection(loaded),"sample_already_catalogued")
        for key,bundle in loaded.items():
            e.need(bundle["manifest"]["selection"]["plan_sha256"]==report["plan_sha256"],
                   "publication_plan_mismatch")
            if automatic:
                e.need(bundle["manifest"].get("publication_policy")==policy,"publication_policy_invalid")
        import_validated(temp/"review",merged)
        if automatic:
            merged_index=e.read_json(merged/"index.json",1024*1024)
            merged_index["publication_policy"]=policy
            merged_index["publication_status"]="standing_owner_policy_automatic_validation"
            for entry in merged_index["real_examples"]:
                if entry["match_id"] in loaded:
                    entry["review_state"]="automated_content_validation_under_standing_owner_policy"
            (merged/"index.json").write_bytes(e.dumps(merged_index))
        files={"docs/ml/data_catalog/index.json":(merged/"index.json").read_bytes()}
        for key in loaded:
            for name in (*e.FILES,"manifest.json"):
                path="samples/match_"+key+"/"+name
                files["docs/ml/data_catalog/"+path]=(merged/path).read_bytes()
        files["docs/ml/data_catalog/acquisition-plans/"+report["plan_sha256"]+".json"]=e.dumps(plan)
        receipt={"stage_ticket":stage_ticket,"source_commit":source_sha,"archive_sha256":e.sha(archive),
                 "report":report,**context}
        files["docs/ml/data_catalog/acquisition-reviews/"+stage_ticket+".json"]=e.dumps(receipt)
    e.need(sum(map(len,files.values()))<=32*1024*1024,"publication_output_limit")
    for value in files.values():
        e.inspect_content(json.loads(value,object_pairs_hook=e.unique),(os.environ.get("GH_TOKEN",""),))
    return files


def prepare(archive,checkout,source_sha,stage_ticket,approval):
    """Compatibility path for the optional two-step workflow."""
    approval=review(approval)
    e.need(e.sha(archive)==approval["archive_sha256"],"review_digest_mismatch")
    return prepare_validated(archive,checkout,source_sha,stage_ticket,{"review":approval})


def publish_prepared(api,files,source_sha,stage_ticket,description,*,automatic=False):
    ticket(stage_ticket)
    e.need(re.fullmatch("[a-f0-9]{40}",source_sha) and type(files)is dict and files,"publication_invalid")
    for path,data in files.items():
        e.need(path.startswith("docs/ml/data_catalog/") and ".." not in Path(path).parts and
               path.endswith(".json"),"publication_path")
        e.inspect_content(json.loads(data,object_pairs_hook=e.unique),(getattr(api,"token",""),))
    if automatic: e.need(not pending_samples(api),"pending_sample_pr")
    branch="samples/first20-"+stage_ticket
    current=api.call("GET","git/ref/heads/ML")
    e.need(current["object"]["sha"]==source_sha,"ml_changed_before_publication")
    e.need(api.call("GET","git/ref/heads/"+branch) is None,"sample_branch_exists")
    commit=api.call("GET","git/commits/"+source_sha)
    tree=api.call("POST","git/trees",{"base_tree":commit["tree"]["sha"],"tree":[
        {"path":path,"mode":"100644","type":"blob","content":data.decode("utf-8")}
        for path,data in sorted(files.items())]})
    created=api.call("POST","git/commits",{"message":"Add sports sample batch "+stage_ticket,
        "tree":tree["sha"],"parents":[source_sha]})
    e.need(api.call("GET","git/ref/heads/ML")["object"]["sha"]==source_sha,"ml_changed_before_publication")
    api.call("POST","git/refs",{"ref":"refs/heads/"+branch,"sha":created["sha"]})
    return api.call("POST","pulls",{"base":"ML","head":branch,
        "title":"ML: real sports sample batch "+stage_ticket,"body":description})


def publish(api,archive,checkout,source_sha,stage_ticket,approval):
    files=prepare(archive,checkout,source_sha,stage_ticket,approval)
    description=("Reviewed staged sports JSON; no merge performed.\n\nArchive SHA-256: "+
        approval["archive_sha256"]+"\nContent review: "+approval["content_evidence"]+
        "\nSource redistribution rights review: "+approval["rights_evidence"]+
        "\n\nOriginal identities and values retained; missing coverage is recorded in the catalog.")
    return publish_prepared(api,files,source_sha,stage_ticket,description)


def publish_automatic(api,archive,checkout,source_sha,stage_ticket):
    with diag.at("publication_validation"):
        policy=standing_policy()
        files=prepare_validated(archive,checkout,source_sha,stage_ticket,{
            "publication_policy":policy,"automatic_validation":"integrity_and_secret_contact_checks_passed",
            "third_party_rights":"not_certified_owner_responsibility"},automatic=True)
    description=("Original sports records published under standing owner policy "+policy["policy_id"]+
        ". No per-batch manual attestations or automatic merge.\n\nArchive SHA-256: "+e.sha(archive)+
        "\nAutomatic integrity/secret/contact checks passed. Original identities and values retained; "+
        "missing coverage is recorded in the catalog.\n\nRepository owner permission does not establish "+
        "third-party redistribution rights. Content eligibility remains the project owner's responsibility.")
    with diag.at("github_publication"):
        return publish_prepared(api,files,source_sha,stage_ticket,description,automatic=True)


def main():
    try:
        with Path(os.environ["SAMPLE_ARCHIVE"]).open("rb") as stream: data=stream.read(LIMIT+1)
        e.need(len(data)<=LIMIT,"publication_input_limit")
        api=GitHub(os.environ["GITHUB_REPOSITORY"],os.environ["GH_TOKEN"])
        if sys.argv[1:]==["--automatic"]:
            result=publish_automatic(api,data,ROOT,os.environ["REVIEWED_SHA"],os.environ["STAGE_TICKET"])
        else:
            e.need(not sys.argv[1:],"publication_arguments")
            approval=e.read_json(os.environ["REVIEW_FILE"],4096)
            result=publish(api,data,ROOT,os.environ["REVIEWED_SHA"],os.environ["STAGE_TICKET"],approval)
        e.need(type(result.get("number"))is int,"publication_response")
        print("sample_pr_created:"+str(result["number"]))
        return 0
    except Exception:
        print("sample_pr_publication_failed",file=sys.stderr)
        return 1


if __name__=="__main__": raise SystemExit(main())
