"""Offline checks for the manual read-only export workflow."""
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_workflow_is_manual_main_only_and_shell_is_valid():
    path = ROOT / ".github/workflows/production-full-match-sample.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    job = next(iter(workflow["jobs"].values()))
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert int(job["timeout-minutes"]) <= 5
    steps = job["steps"]
    for step in steps:
        if "run" in step:
            subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
    shell = "\n".join(step.get("run", "") for step in steps)
    for required in ("StrictHostKeyChecking=yes", "SSH_FINGERPRINT",
                     "IdentityAgent=none", "ForwardAgent=no", "ClearAllForwardings=yes",
                     "production_full_match_sample.py", "full_match_export.py",
                     "match-sample.json", "ssh-keyscan", "timeout"):
        assert required in shell
    upload = [step for step in steps if "actions/upload-artifact@" in step.get("uses", "")]
    assert len(upload) == 1
    assert upload[0]["with"]["retention-days"] == "1"
    assert "secrets." not in upload[0]["with"]["path"]


def test_offline_ci_covers_production_driver():
    workflow = yaml.load(
        (ROOT / ".github/workflows/full-match-validation-tests.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "pull_request" in workflow["on"]
    assert "secrets." not in str(workflow)
    commands = "\n".join(step.get("run", "") for job in workflow["jobs"].values()
                         for step in job["steps"])
    assert "production_full_match_sample.py" in commands
