"""Actions-side resumable client for the existing protected SSH runtime."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.export_match_samples import inspect_content
from scripts.sample_policy import standing_policy

from tennis_predictor.ml.canonical_layer.evidence import encoded
from tennis_predictor.ml.identity_review import pg_export
from tennis_predictor.ml.identity_review.staging import extract, review_dir, uuid_text
from tennis_predictor.ml.identity_review.workbook import create_workbook

SCRIPT = Path(pg_export.__file__)
REMOTE_COMMAND = (
    "cd /root/Tennis_pelmen_model && timeout --kill-after=5s 1520s .venv/bin/python -I -B -"
)


class Interrupted(OSError):
    pass


class LiveSource:
    def __init__(self, output, operation, start):
        if not re.fullmatch(r"[0-9]+", operation):
            raise ValueError("Invalid workflow run ID")
        self.source_id = f"production-live-keyset:{operation}"
        self.start = start
        self.boundary = output / "boundary.json"
        self.process = None
        self.retries = 3

    def read(self):
        line = self.process.stdout.readline(pg_export.MAX_FRAME + 1)
        if not line:
            raise Interrupted("SSH export ended before completion")
        if len(line) > pg_export.MAX_FRAME or not line.endswith(b"\n"):
            raise ValueError("Invalid export frame size")
        message = json.loads(line)
        if not isinstance(message, dict) or len(message) != 1:
            raise ValueError("Invalid export frame")
        return message

    def open(self, after):
        pinned = self.boundary.exists()
        upper = json.loads(self.boundary.read_bytes())["upper"] if pinned else None
        if after is not None and not pinned:
            raise ValueError("Missing boundary for persisted cursor")
        request = {"pinned": pinned, "upper": upper, "after": after}
        literal = f"REQUEST = {request!r}\n".encode()
        self.process = self.start(literal + SCRIPT.read_bytes())
        message = self.read()
        if set(message) != {"upper"}:
            raise ValueError("Missing export boundary")
        received = message["upper"]
        if received is not None:
            uuid_text(received)
        if pinned and received != upper:
            raise ValueError("Export boundary changed")
        if not pinned:
            pending = self.boundary.with_suffix(".pending")
            pending.write_bytes(encoded(message))
            pending.replace(self.boundary)
        self.upper = received

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdout.close()
            self.process = None

    def page(self, after):
        while True:
            try:
                if self.process is None:
                    self.open(after)
                message = self.read()
                if message == {"done": True}:
                    if self.process.wait(timeout=15):
                        raise Interrupted("SSH returned a failure")
                    self.close()
                    return []
                if message == {"pause": True}:
                    raise Interrupted("Remote extraction window elapsed")
                rows = message.get("rows")
                if not isinstance(rows, list) or not 1 <= len(rows) <= pg_export.PAGE_SIZE:
                    raise ValueError("Invalid export row count")
                for row in rows:
                    if not isinstance(row, dict) or set(row) != {*pg_export.FIELDS, "source_ref"}:
                        raise ValueError("Unexpected export projection")
                    mid = uuid_text(row["match_id"])
                    if (
                        self.upper is None
                        or mid > self.upper
                        or row["source_ref"] != f"public.matches/{mid}"
                    ):
                        raise ValueError("Invalid exported row boundary")
                inspect_content(rows)
                return rows
            except (Interrupted, BrokenPipeError):
                self.close()
                if not self.retries:
                    raise
                self.retries -= 1


def ssh_start(payload):
    private = Path(os.environ["PLAYER_REVIEW_SSH_DIR"])
    host, user = os.environ["SSH_HOST"], os.environ["SSH_USER"]
    args = [
        "ssh",
        "-F",
        "/dev/null",
        "-T",
        "-p",
        "22",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={private / 'known_hosts'}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "VerifyHostKeyDNS=no",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-i",
        str(private / "key"),
        "-l",
        user,
        "--",
        host,
        REMOTE_COMMAND,
    ]
    process = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        with process.stdin:
            process.stdin.write(payload)
    except BrokenPipeError:
        process.stdout.close()
        process.wait()
        raise
    return process


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    standing_policy()
    output = review_dir(args.output)
    source = LiveSource(output, os.environ["GITHUB_RUN_ID"], ssh_start)
    stage = output / "evidence.sqlite"
    try:
        for _ in range(4):
            try:
                state = extract(stage, source)
            finally:
                source.close()
            if state["dataset"]:
                create_workbook(stage, output / "registry.sqlite", output / "player-review.xlsx")
                print("Identity evidence scan completed; review workbook is ready.")
                return 0
    except Exception:  # noqa: BLE001 -- suppress evidence and secrets in Actions logs
        print("player_registry_export_failed; no workbook published", file=sys.stderr)
        return 1
    print("player_registry_export_budget_exhausted; no workbook published", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
