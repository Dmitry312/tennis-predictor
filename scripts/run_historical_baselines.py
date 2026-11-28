"""Run the registered splits against a frozen local source bundle or the real catalog."""

import argparse
import json
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.ml.baselines.evaluation import HOLDOUT, TRAIN, WARMUP, run
from tennis_predictor.ml.baselines.projection import project
from tennis_predictor.ml.canonical_layer.evidence import encoded, fingerprint, instant
from tennis_predictor.ml.canonical_layer.identity import PinnedIdentities
from tennis_predictor.ml.canonical_layer.source import CatalogAdapter, read
from tennis_predictor.ml.canonical_layer.storage import checksum
from tennis_predictor.ml.identity_review import staging
from tennis_predictor.ml.identity_review.bulk import import_approved_groups
from tennis_predictor.ml.ratings.engine import CODE as RATING_CODE

FILES = ("metadata.jsonl", "details.jsonl", "registry.sqlite")
MODULES = (Path(__file__), *sorted((ROOT / "src/tennis_predictor/ml/baselines").glob("*.py")))


def lines(path):
    with path.open("rb") as stream:
        for line in stream:
            yield json.loads(line)


def bundle(folder):
    manifest = json.loads((folder / "source.json").read_bytes())
    if manifest["format"] != "historical-source-v1" or set(manifest["files"]) != set(FILES):
        raise ValueError("Wrong source bundle format")
    for name in FILES:
        if checksum(folder / name) != manifest["files"][name]:
            raise ValueError(f"Corrupt frozen source file: {name}")
    pin = PinnedIdentities(folder / "registry.sqlite", manifest["registry_revision"])
    if pin.token != manifest["registry_token"]:
        raise ValueError("Wrong registry snapshot token")
    as_of = instant(manifest["as_of"])
    if as_of is None:
        raise ValueError("Source snapshot needs an actual timestamp")
    return lines(folder / FILES[0]), lines(folder / FILES[1]), pin, as_of, manifest


def catalog(root, temp):
    adapter = CatalogAdapter(root)
    metadata, details, manifests = [], [], {}
    for mid, manifest_sha in adapter.entries():
        manifests[mid] = manifest_sha
        meta, _ = adapter.match_metadata(mid, manifest_sha)
        meta = dict(meta)
        manifest, folder = adapter.manifest(mid, manifest_sha)
        evidence, _ = read(folder / "nbbet_details.json", manifest["files"]["nbbet_details.json"])
        metadata.append(meta)
        details.extend(row for revision in evidence["versions"] for row in revision["rows"])
    stage, registry = temp / "stage.sqlite", temp / "registry.sqlite"
    state = staging.extract(stage, staging.CatalogSource(root))
    with closing(staging.connection(stage)) as db:
        groups = {
            r[0]: "catalog-" + r[0]
            for r in db.execute("SELECT DISTINCT group_id FROM observations")
        }
    import_approved_groups(
        stage,
        registry,
        {
            "dataset": state["dataset"],
            "parent": "root",
            "groups": groups,
            "approval_ref": "local catalog fixture only",
            "recorded_at": "2026-11-27T07:37:00Z",
        },
    )
    pin = PinnedIdentities(registry, 1)
    # Use the latest recorded details receipt rather than fabricate source availability.
    as_of = max(
        instant(row.get("fetched_at"), naive_utc=True) or datetime.min.replace(tzinfo=UTC)
        for row in details
    )
    return (
        metadata,
        details,
        pin,
        as_of,
        {
            "format": "catalog-smoke-v1",
            "manifest_set": fingerprint(manifests),
            "as_of": as_of.isoformat(),
            "registry_revision": pin.revision,
            "registry_token": pin.token,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="Frozen historical-source-v1 folder")
    source.add_argument("--catalog", type=Path, help="Real 20-match smoke with test registry")
    parser.add_argument("--output", type=Path, default=ROOT / "data/ml/historical-baselines")
    parser.add_argument(
        "--held-out", type=Path, help="Frozen validation JSON for the identical source"
    )
    args = parser.parse_args()
    if args.catalog and args.held_out:
        parser.error("The tiny catalog holdout is reserved")
    with tempfile.TemporaryDirectory() as tmp:
        metadata, details, identity, as_of, manifest = (
            bundle(args.source) if args.source else catalog(args.catalog, Path(tmp))
        )
        source_id = {
            "source_fingerprint": fingerprint(manifest),
            "source_type": manifest["format"],
            "source_as_of": as_of.isoformat(),
            "registry_token": identity.token,
            "registry_revision": identity.revision,
            "rating_code": RATING_CODE,
            "baseline_code": fingerprint([checksum(path) for path in MODULES]),
            "libraries": {name: version(name) for name in ("numpy", "scikit-learn", "catboost")},
        }
        frozen = None
        if args.held_out:
            frozen = json.loads(args.held_out.read_bytes())
            required = {"assessment": "validation", "snapshot": source_id}
            if any(frozen.get(k) != v for k, v in required.items()) or as_of.date() <= HOLDOUT:
                parser.error("A frozen validation output on the exact code and source is required")
        date_limit = min(as_of.date(), HOLDOUT) if frozen is None else as_of.date()
        metadata = (
            row
            for row in metadata
            if not row.get("match_date") or row["match_date"] < date_limit.isoformat()
        )
        matches, waterfall = project(metadata, details, identity, as_of)
        report = run(
            matches,
            source_id,
            args.output / ("holdout" if frozen else "validation"),
            as_of.date(),
            frozen=frozen,
            train_start=WARMUP if args.catalog else TRAIN,
        )
        report["waterfall"] = waterfall
        sys.stdout.buffer.write(encoded(report))


if __name__ == "__main__":
    main()
