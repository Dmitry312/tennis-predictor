"""Build fixed reviewed workflow requests without exposing raw records."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import export_match_samples as e
from scripts import build_sample_acquisition_payload as b
from scripts.review_staging import review, ticket
from infra.audit.sample_diagnostics import at


def parameters(environ):
    mode=environ["OPERATION"]
    e.need(mode in ("stage","release","cleanup"),"operation_invalid")
    source_sha=environ["REVIEWED_SHA"]
    e.need(source_sha==environ["SELECTED_SHA"],"reviewed_sha")
    identifier=ticket(environ["STAGE_TICKET"])
    approval=None
    if mode=="release":
        approval=review({"content_reviewed":environ.get("CONTENT_REVIEWED")=="true",
            "rights_reviewed":environ.get("RIGHTS_REVIEWED")=="true",
            "content_evidence":environ.get("CONTENT_EVIDENCE",""),
            "rights_evidence":environ.get("RIGHTS_EVIDENCE",""),
            "archive_sha256":environ.get("STAGE_DIGEST","")})
    return dict(project=environ["PROJECT_PATH"],sha=source_sha,
        offset=int(environ.get("BATCH_OFFSET","0")),batch_size=int(environ.get("BATCH_SIZE","2")),
        pages=4,after=environ.get("AFTER_UUID"),plan_path=environ.get("PLAN_PATH"),
        manifest_path=environ.get("MANIFEST_PATH"),operation=mode,stage_ticket=identifier,
        approval=approval,allow_existing_root=environ.get("ALLOW_EXISTING_ROOT")=="true")


def main():
    try:
        with at("runner_request"):
            return execute_request()
    except Exception:
        return 1


def execute_request():
        options=parameters(os.environ)
        if options["approval"]:
            # Review metadata only, never raw samples. Used by the isolated publish step.
            with Path(os.environ["REVIEW_FILE"]).open("x") as stream:
                json.dump(options["approval"],stream)
        sys.stdout.write(b.build(**options))
        return 0


if __name__=="__main__": raise SystemExit(main())
