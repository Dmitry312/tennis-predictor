"""Run E03A then E03B on one verified pre-holdout snapshot."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.run_historical_baselines import MODULES, bundle

from tennis_predictor.ml.baselines.evaluation import HOLDOUT
from tennis_predictor.ml.baselines.optimization import optimize
from tennis_predictor.ml.baselines.projection import project
from tennis_predictor.ml.canonical_layer.evidence import encoded, fingerprint
from tennis_predictor.ml.canonical_layer.storage import checksum
from tennis_predictor.ml.ratings.engine import CODE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    args = parser.parse_args()
    metadata, details, registry, as_of, manifest = bundle(args.source)
    if manifest.get("period") != "validation-only":
        raise ValueError("E03A/E03B require a source restricted to 2021–2025")
    meta = (
        row
        for row in metadata
        if not row.get("match_date") or row["match_date"] < HOLDOUT.isoformat()
    )
    matches, waterfall = project(meta, details, registry, as_of)
    source = {
        "fingerprint": fingerprint(manifest),
        "manifest": manifest,
        "registry_token": registry.token,
        "rating_code": CODE,
        "experiment_code": fingerprint([checksum(p) for p in (*MODULES, Path(__file__))]),
    }
    args.reports.mkdir(parents=True, exist_ok=True)

    def save(name, result):
        path = args.reports / (name + ".json")
        temp = path.with_suffix(".new")
        temp.write_bytes(encoded({**result, "waterfall": waterfall}))
        temp.replace(path)

    optimize(matches, source, args.output, save)


if __name__ == "__main__":
    main()
