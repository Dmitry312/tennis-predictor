"""Import the exact owner-approved group set without querying production."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.ml.canonical_layer.evidence import encoded
from tennis_predictor.ml.canonical_layer.storage import checksum
from tennis_predictor.ml.identity_review.bulk import import_approved_groups
from tennis_predictor.ml.identity_review.staging import review_dir

DATASET = "c01abdfa58a88bcf948283cbf8f1a24d1d3fe052556c0d203e2cd944c1a54d7c"
ARTIFACT = 15140813328
ARTIFACT_SHA256 = "11f62215eb8a8d4e9c3e26fefc26a17ce5248c88a8ee014e198e0a11034a41d7"
DEFAULT = ROOT / "data/ml/identity-import"


def download_evidence(output):
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        directory = Path(directory)
        archive = directory / "artifact.zip"
        with archive.open("wb") as stream:
            subprocess.run(
                ["gh", "api", f"repos/Dmitry312/tennis-predictor/actions/artifacts/{ARTIFACT}/zip"],
                stdout=stream,
                check=True,
                timeout=600,
            )
        if checksum(archive) != ARTIFACT_SHA256:
            raise ValueError("Original GitHub artifact checksum mismatch")
        with ZipFile(archive) as packaged:
            files = [
                member
                for member in packaged.infolist()
                if Path(member.filename).name == "evidence.sqlite"
            ]
            if len(files) != 1 or files[0].file_size > 1024**3:
                raise ValueError("Expected one bounded evidence.sqlite member")
            staged = directory / "evidence.sqlite"
            with packaged.open(files[0]) as source, staged.open("xb") as target:
                shutil.copyfileobj(source, target, 1024**2)
        staged.chmod(0o600)
        staged.replace(output)


def approved_packet(path):
    approval = json.loads(path.read_bytes())
    if approval["dataset"] == DATASET:
        initial, alternate = approval.pop("initial_groups"), approval.pop("alternative_groups")
        if (
            len(initial) != 7271
            or len(set(initial)) != 7271
            or len(alternate) != 232
            or len(set(alternate)) != 232
        ):
            raise ValueError(
                "Expected the exact 7,271 initial and 232 alternate-spelling group IDs"
            )
        allowed = set(initial) | set(alternate)
        if "groups" not in approval:
            approval["groups"] = {gid: f"group-{gid}" for gid in sorted(allowed)}
        if set(approval["groups"]) != allowed:
            raise ValueError("Group assignments must equal the approved union")
    return approval


def publish_manifest(registry, result):
    folder = review_dir(registry.parent / "registry-manifests")
    target = folder / f"{result['token']}.json"
    content = encoded(result)
    with tempfile.NamedTemporaryFile(dir=folder) as stream:
        stream.write(content)
        stream.flush()
        try:
            os.link(stream.name, target)
        except FileExistsError:
            if target.read_bytes() != content:
                raise ValueError("Existing registry manifest differs") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT / "evidence.sqlite")
    parser.add_argument("--approval", type=Path, default=DEFAULT / "approval.json")
    parser.add_argument("--registry", type=Path, default=DEFAULT / "registry.sqlite")
    args = parser.parse_args()
    if not args.approval.is_file():
        parser.error(f"Missing exact owner group assignments: {args.approval}")
    packet = approved_packet(args.approval)
    if not args.evidence.is_file():
        if packet["dataset"] != DATASET:
            parser.error("Supply saved evidence for this dataset")
        review_dir(args.evidence.parent)
        download_evidence(args.evidence)
    result = import_approved_groups(args.evidence, args.registry, packet)
    publish_manifest(args.registry, result)
    sys.stdout.buffer.write(encoded(result))


if __name__ == "__main__":
    main()
