"""Bound SSH stdout without printing or uploading source records."""
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import export_match_samples as e
from scripts.sample_delivery import LIMIT
from scripts.review_staging import ticket
from infra.audit.sample_diagnostics import at


def capture(source, environ):
    mode=environ["OPERATION"]
    cap=LIMIT if mode=="release" else 4096
    data=source.read(cap+1)
    e.need(len(data)<=cap,"ssh_output_limit")
    if mode=="release":
        e.need(e.sha(data)==environ["STAGE_DIGEST"],"review_digest_mismatch")
        with Path(environ["SAMPLE_ARCHIVE"]).open("xb") as stream:
            os.chmod(environ["SAMPLE_ARCHIVE"],0o600)
            stream.write(data)
    elif mode=="stage":
        value=json.loads(data,object_pairs_hook=e.unique)
        e.need(set(value)=={"ticket","archive_sha256","source_sha","created_at","exported_count"},
               "stage_response")
        e.need(ticket(value["ticket"])==environ["STAGE_TICKET"] and
               value["source_sha"]==environ["REVIEWED_SHA"] and
               re.fullmatch("[a-f0-9]{64}",value["archive_sha256"]) and
               type(value["exported_count"])is int and 1<=value["exported_count"]<=5,"stage_response")
        # Whitelist only opaque identifiers/counts for the public summary.
        summary="Private samples staged. Ticket: "+value["ticket"]+"\nArchive SHA-256: "+value["archive_sha256"]+"\n"
        with Path(environ["GITHUB_STEP_SUMMARY"]).open("a") as stream: stream.write(summary)
    else:
        e.need(mode=="cleanup" and not data,"cleanup_response")


if __name__=="__main__":
    try:
        with at("runner_capture"):
            capture(sys.stdin.buffer,os.environ)
    except Exception: raise SystemExit(1)
