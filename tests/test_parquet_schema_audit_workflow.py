"""Validate the manual workflow without executing SSH or accessing production."""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_manual_workflow_and_shell_syntax():
    path = ROOT / ".github/workflows/production-parquet-schema-audit.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    job = next(iter(workflow["jobs"].values()))
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    assert int(job["timeout-minutes"]) <= 5
    for step in job["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"actions/checkout@[a-f0-9]{40}", step["uses"])
            assert step["with"]["persist-credentials"] == "false"
        if "run" in step:
            subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
    shell = "\n".join(step.get("run", "") for step in job["steps"])
    for required in (
        "ssh-keyscan",
        "-t ed25519",
        "actual_fingerprint",
        '"$SSH_FINGERPRINT"',
        "StrictHostKeyChecking=yes",
        "ForwardAgent=no",
        "ClearAllForwardings=yes",
        "IdentityAgent=none",
        "-I -B -",
        "110s",
        "production_parquet_schema_audit.py",
    ):
        assert required in shell
    for forbidden in ("scp ", "sftp ", "docker ", "sudo ", "workflow run", "git push"):
        assert forbidden not in shell


def test_ci_yaml_and_shell_syntax():
    workflow = yaml.load(
        (ROOT / ".github/workflows/parquet-schema-audit-tests.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "pull_request" in workflow["on"]
    assert "secrets." not in str(workflow)
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
