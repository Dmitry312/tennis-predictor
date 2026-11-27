"""Import the exact owner-approved group set without querying production."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from time import perf_counter
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.ml.canonical_layer.evidence import encoded
from tennis_predictor.ml.canonical_layer.storage import checksum
from tennis_predictor.ml.identity_review.bulk import import_approved_groups
from tennis_predictor.ml.identity_review.staging import connection, review_dir

DATASET = "c01abdfa58a88bcf948283cbf8f1a24d1d3fe052556c0d203e2cd944c1a54d7c"
ARTIFACT = 15140813328
ARTIFACT_SHA256 = "11f62215eb8a8d4e9c3e26fefc26a17ce5248c88a8ee014e198e0a11034a41d7"
EXPECTED = {
    "players_by_gender": {"M": 5176, "F": 2723},
    "aliases": 8131,
    "observations": 351928,
    "matches": 175964,
}
FINAL_APPROVAL = {
    "dataset": DATASET,
    "parent": "root",
    "recorded_at": "2026-11-27T10:37:00+03:00",
    "approval_ref": "https://github.com/Dmitry312/tennis-predictor/pull/97#issuecomment-7212660453",
}
DEFAULT = ROOT / "data/ml/identity-import"


def download_evidence(output, artifact=ARTIFACT):
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        directory = Path(directory)
        archive = directory / "artifact.zip"
        with archive.open("wb") as stream:
            subprocess.run(
                ["gh", "api", f"repos/Dmitry312/tennis-predictor/actions/artifacts/{artifact}/zip"],
                stdout=stream,
                check=True,
                timeout=600,
            )
        if artifact == ARTIFACT and checksum(archive) != ARTIFACT_SHA256:
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
    if path is None:
        return FINAL_APPROVAL, EXPECTED
    packet = json.loads(path.read_bytes())
    if packet["dataset"] == DATASET and packet["parent"] == "root":
        raise ValueError("Use the default sealed full approval for the initial original dataset")
    return packet, None


def verify_indexed_lookup(registry, revision):
    query = (
        "SELECT player_id FROM bulk_aliases "
        "WHERE revision=? AND source=? AND gender=? AND normalized=?"
    )
    with closing(connection(registry.resolve().as_uri() + "?mode=ro")) as db:
        plan = [
            row[3]
            for row in db.execute("EXPLAIN QUERY PLAN " + query, (revision, "nbbet", "M", ""))
        ]
        if not any(
            "SEARCH bulk_aliases USING INDEX" in row and "normalized=?" in row for row in plan
        ):
            raise ValueError("Alias point lookup does not use the scoped index")
        start = perf_counter()
        count = 0
        for row in db.execute(
            "SELECT source,gender,normalized,player_id FROM bulk_aliases WHERE revision=?",
            (revision,),
        ):
            if db.execute(query, (revision, *tuple(row)[:3])).fetchone()[0] != row["player_id"]:
                raise ValueError("Indexed alias lookup returned inconsistent evidence")
            count += 1
    return {"indexed_lookups": count, "lookup_seconds": round(perf_counter() - start, 3)}


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
    parser.add_argument("--approval", type=Path, help="Explicit correction or fixture approval")
    parser.add_argument("--registry", type=Path, default=DEFAULT / "registry.sqlite")
    parser.add_argument("--saved-evidence-artifact", type=int, default=ARTIFACT)
    parser.add_argument("--verify-repeat", action="store_true")
    args = parser.parse_args()
    packet, expected = approved_packet(args.approval)
    if not args.evidence.is_file():
        if packet["dataset"] != DATASET:
            parser.error("Supply saved evidence for this dataset")
        if args.saved_evidence_artifact <= 0:
            parser.error("Artifact ID must be positive")
        review_dir(args.evidence.parent)
        download_evidence(args.evidence, args.saved_evidence_artifact)
    start = perf_counter()
    result = import_approved_groups(args.evidence, args.registry, packet, expected=expected)
    report = {
        **result,
        "import_seconds": round(perf_counter() - start, 3),
        "registry_bytes": args.registry.stat().st_size,
    }
    if args.verify_repeat:
        before = checksum(args.registry)
        start = perf_counter()
        repeated = import_approved_groups(args.evidence, args.registry, packet, expected=expected)
        if repeated != result or checksum(args.registry) != before:
            raise ValueError("Registry changed on identical import")
        report.update(repeat_seconds=round(perf_counter() - start, 3), idempotent=True)
        report.update(verify_indexed_lookup(args.registry, result["revision"]))
    publish_manifest(args.registry, result)
    sys.stdout.buffer.write(encoded(report))


if __name__ == "__main__":
    main()
