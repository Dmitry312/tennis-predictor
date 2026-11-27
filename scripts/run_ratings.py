"""Build local rating snapshots and run bounded, chronological comparisons."""

import argparse
import json
import sys
from contextlib import closing
from dataclasses import asdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.ml.aggregation.batch import SnapshotReader
from tennis_predictor.ml.canonical_layer.evidence import encoded
from tennis_predictor.ml.canonical_layer.identity import PinnedIdentities
from tennis_predictor.ml.ratings.algorithms import MODELS, Config
from tennis_predictor.ml.ratings.engine import CODE, Reader, build
from tennis_predictor.ml.ratings.evaluate import Splits, evaluate, variants
from tennis_predictor.ml.ratings.inputs import load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("match-data", "canonical", "registry", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--match-data-version")
    parser.add_argument("--registry-revision", type=int, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--splits",
        nargs=5,
        type=date.fromisoformat,
        metavar=("WARMUP", "TRAIN", "VALIDATION", "TEST", "END"),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--compare", action="store_true")
    group.add_argument("--held-out", type=Path, help="Frozen validation report from --compare")
    args = parser.parse_args()
    if (args.compare or args.held_out) and (not args.splits or args.config):
        parser.error("Comparison and final evaluation require --splits and use registered configs")
    splits = Splits(*args.splits) if args.splits else None
    snapshot = SnapshotReader(args.match_data, args.match_data_version)
    registry = PinnedIdentities(args.registry, args.registry_revision)
    matches, source = load(snapshot, args.canonical, registry)
    report = {"algorithm_code": CODE, "source": source, "eligible_results": len(matches)}
    choices = (
        list(variants())
        if args.compare or args.held_out
        else [
            (
                "baseline",
                Config(**json.loads(args.config.read_bytes())) if args.config else Config(),
                MODELS,
            )
        ]
    )
    selected = None
    if splits:
        report["splits"] = {k: v.isoformat() for k, v in asdict(splits).items()}
        matches = [m for m in matches if splits.warmup <= m.day < splits.end]
    if args.held_out:
        frozen = json.loads(args.held_out.read_bytes())
        if any(frozen.get(k) != v for k, v in report.items()):
            parser.error("Frozen validation report must match code, source and splits")
        selected = frozen["selected_on_validation"]
        if set(selected) != set(MODELS):
            parser.error("A frozen validation selection is required per algorithm")
    results = {}
    for name, config, models in choices:
        if selected is not None:
            models = tuple(model for model in models if selected[model] == name)
            if not models:
                continue
        folder = args.output / name
        # Validation runs never expose the holdout in a rating output either.
        cohort = (
            [m for m in matches if m.day < splits.test] if splits and not args.held_out else matches
        )
        info = build(folder, cohort, source, config)
        if splits:
            with closing(Reader(folder, info["version"]).connect()) as db:
                info["metrics"] = evaluate(db, splits, models=models, held_out=bool(args.held_out))
        results[name] = {**info, "config": asdict(config)}
    if args.compare:
        winners = {}
        for model in MODELS:
            candidates = []
            for name, info in results.items():
                metric = info["metrics"].get("validation", {}).get(model, {}).get("all", {})
                if metric.get("n"):
                    candidates.append((metric["log_loss"], name != "baseline", name))
            if candidates:
                winners[model] = min(candidates)[2]
        report["selected_on_validation"] = winners
    report["runs"] = results
    sys.stdout.buffer.write(encoded(report))


if __name__ == "__main__":
    main()
