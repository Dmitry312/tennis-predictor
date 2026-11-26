#!/usr/bin/env python3
"""Build or reuse a verified local Canonical Data Layer snapshot."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.ml.canonical_layer.contracts import MODES
from tennis_predictor.ml.canonical_layer.evidence import encoded
from tennis_predictor.ml.canonical_layer.source import CatalogAdapter
from tennis_predictor.ml.canonical_layer.storage import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "docs/ml/data_catalog")
    parser.add_argument("--output", type=Path, default=ROOT / "data/ml/canonical")
    parser.add_argument("--config", type=Path, default=ROOT / "docs/ml/canonical_layer_v1.json")
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--prediction-cutoff")
    parser.add_argument("--json", action="store_true", help="Print the current run report as JSON.")
    args = parser.parse_args()
    config = json.loads(args.config.read_bytes())
    if args.mode:
        config["mode"] = args.mode
    if args.prediction_cutoff:
        config["prediction_cutoff"] = args.prediction_cutoff
    report = build(CatalogAdapter(args.catalog), args.output, config)
    if args.json:
        sys.stdout.write(encoded(report).decode())
        return
    counts = ", ".join(f"{key}={value}" for key, value in report["changes"].items())
    rows = ", ".join(f"{table}={count}" for table, count in report["rows"].items())
    snapshot = args.output / report["mode"] / "versions" / report["version"]
    print(f"{report['mode']}: {report['processed_matches']} matches ({counts})")
    print(
        f"Match-local winners: {report['match_local_winners']}; local labels: {report['confirmed_results']}; eligible targets: {report['eligible_targets']}"
    )
    print(
        f"Missing checks: {report['missing_checks']}; conflicting checks: {report['conflicting_checks']}"
    )
    print(rows)
    print(f"Snapshot and JSON quality report: {snapshot}")


if __name__ == "__main__":
    main()
