"""Run validation-only raw form experiments on one verified historical bundle."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.run_historical_baselines import bundle, lines

from tennis_predictor.ml.baselines.projection import project
from tennis_predictor.ml.canonical_layer.evidence import encoded, fingerprint
from tennis_predictor.ml.canonical_layer.storage import checksum
from tennis_predictor.ml.player_features.evaluation import evaluate
from tennis_predictor.ml.player_features.projection import facts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metadata, details, registry, as_of, manifest = bundle(args.source, with_stats=True)
    if manifest["period"] != "validation-only":
        raise ValueError("E04 requires verified validation-only source")
    matches, waterfall = project(metadata, details, registry, as_of)
    source = {
        "snapshot": fingerprint(manifest),
        "manifest": manifest,
        "code": fingerprint(
            [
                checksum(Path(__file__)),
                *(
                    checksum(p)
                    for directory in ("player_features", "baselines")
                    for p in sorted((ROOT / "src/tennis_predictor/ml" / directory).glob("*.py"))
                ),
            ]
        ),
    }
    projection = facts(
        matches, lines(args.source / "details.jsonl"), lines(args.source / "stats.jsonl"), as_of
    )
    report = evaluate(matches, projection, source, args.output)
    sys.stdout.buffer.write(encoded({**report, "waterfall": waterfall}))


if __name__ == "__main__":
    main()
