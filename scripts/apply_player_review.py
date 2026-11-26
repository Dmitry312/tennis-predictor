"""Explicitly publish owner decisions to the local identity registry."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.ml.canonical_layer.evidence import encoded
from tennis_predictor.ml.identity_review.registry import import_review
from tennis_predictor.ml.identity_review.workbook import create_workbook


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--registry", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("packet")
    export.add_argument("workbook", type=Path)
    apply = commands.add_parser("apply")
    apply.add_argument("workbook", type=Path)
    apply.add_argument("--reviewer", required=True)
    apply.add_argument("--recorded-at", required=True)
    args = parser.parse_args()
    stage, registry = (args.directory / name for name in ("evidence.sqlite", "registry.sqlite"))
    registry = args.registry or registry
    if args.command == "packet":
        result = create_workbook(stage, registry, args.workbook)
    else:
        result = import_review(
            stage, registry, args.workbook, reviewer=args.reviewer, recorded_at=args.recorded_at
        )
    sys.stdout.buffer.write(encoded(result))


if __name__ == "__main__":
    main()
