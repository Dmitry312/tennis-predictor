"""Build the verified local canonical catalog and aggregate reusable match facts."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.ml.aggregation.batch import CanonicalReader, build
from tennis_predictor.ml.canonical_layer.evidence import encoded
from tennis_predictor.ml.canonical_layer.identity import PinnedIdentities
from tennis_predictor.ml.canonical_layer.source import CatalogAdapter
from tennis_predictor.ml.canonical_layer.storage import build as build_canonical


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "docs/ml/data_catalog")
    parser.add_argument("--canonical", type=Path, default=ROOT / "data/ml/canonical")
    parser.add_argument("--config", type=Path, default=ROOT / "docs/ml/canonical_layer_v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/ml/match-data")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--registry-revision", type=int)
    args = parser.parse_args()
    if args.registry_revision is not None and args.registry is None:
        parser.error("--registry-revision needs --registry")
    identities = PinnedIdentities(args.registry, args.registry_revision) if args.registry else None
    config = json.loads(args.config.read_bytes())
    canonical = build_canonical(
        CatalogAdapter(args.catalog), args.canonical, config, identities=identities
    )
    report = build(
        CanonicalReader(args.canonical / config["mode"], canonical["version"]),
        args.output / config["mode"],
    )
    sys.stdout.buffer.write(encoded(report))


if __name__ == "__main__":
    main()
