"""Reviewed streamed bootstrap. All early failures use fixed safe protocol codes."""
import base64
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys
import tempfile

FILES = ("scripts/acquire_match_samples.py","scripts/export_match_samples.py",
         "infra/audit/full_match_export.py","scripts/review_staging.py","scripts/sample_delivery.py",
         "infra/audit/sample_diagnostics.py")
BOOT_REASONS = frozenset(("payload_invalid","root_policy","project_invalid",
                         "project_unavailable","code_staging","imports_failed"))
_phase="payload_invalid"


def boot_event(status,reason="none"):
    if status not in ("start","ok","error") or reason not in BOOT_REASONS|{"none"}:
        reason="payload_invalid"
    print("sample_diag:remote_bootstrap:"+status+":"+reason,file=sys.stderr,flush=True)


def package_request(payload):
    if len(payload)>1024*1024: raise ValueError()
    request=json.loads(payload).get("request",{})
    if type(request)is not dict: raise ValueError()
    return request


def execute(payload):
    global _phase
    _phase="payload_invalid"
    boot_event("start")
    request=package_request(payload)
    _phase="root_policy"
    if os.geteuid()==0 and request.get("allow_existing_root") is not True: raise ValueError()
    _phase="payload_invalid"
    package=json.loads(payload)
    if set(package["files"])!=set(FILES): raise ValueError()
    if not re.fullmatch("[a-f0-9]{40}",package["sha"]): raise ValueError()
    project=package["project"]
    _phase="project_invalid"
    if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,200}",project) or ".." in Path(project).parts:
        raise ValueError()
    if (project=="/root" or project.startswith("/root/")) and request.get("allow_existing_root") is not True:
        raise ValueError()
    _phase="project_unavailable"
    if not Path(project).is_dir(): raise ValueError()
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(RuntimeError()))
    _phase="code_staging"
    with tempfile.TemporaryDirectory(prefix="tennis-reviewed-code-") as tmp:
        for name,content in package["files"].items():
            if type(content)is not str or len(content)>128*1024: raise ValueError()
            path=Path(tmp)/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(content)
        sys.path.insert(0,tmp)
        _phase="imports_failed"
        from infra.audit.sample_diagnostics import at
        from scripts.acquire_match_samples import run
        boot_event("ok")
        operation=request.get("operation","download")
        if operation in ("stage","download"):
            run(request,project,package["sha"])
        else:
            from scripts.review_staging import release,cleanup
            if operation=="release":
                with at("stage_release"):
                    data=release(request["ticket"],request["approval"]["archive_sha256"],
                                 package["sha"],request["approval"])
                sys.stdout.buffer.write(data)
            elif operation=="cleanup":
                with at("stage_cleanup"): cleanup(request["ticket"])
            else:
                _phase="payload_invalid"
                raise ValueError()


def main(encoded):
    global _phase
    _phase="payload_invalid"
    logging.disable(logging.CRITICAL)
    try:
        execute(base64.b64decode(encoded,validate=True))
    except Exception as exc:
        diag=sys.modules.get("infra.audit.sample_diagnostics")
        if diag is None or not diag.reported(exc):
            boot_event("error",_phase if _phase in BOOT_REASONS else "payload_invalid")
        return 1
    return 0
