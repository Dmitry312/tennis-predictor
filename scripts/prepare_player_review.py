"""Prepare a local owner-review workbook from a frozen identity evidence source."""

import argparse
import os
import sys
from pathlib import Path
from time import sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.ml.canonical_layer.evidence import encoded
from tennis_predictor.ml.identity_review.staging import (
    VERSION,
    CatalogSource,
    extract,
    postgres_from_environment,
)
from tennis_predictor.ml.identity_review.workbook import create_workbook


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "docs/ml/data_catalog")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--registry", type=Path)
    args = parser.parse_args()
    mode = os.environ.get("IDENTITY_SOURCE", "catalog")
    if mode == "catalog":
        source = CatalogSource(args.catalog)
        pace = None
    elif mode == "postgres":
        source = postgres_from_environment()

        def pace():
            sleep(0.1)
    else:
        raise ValueError("Unknown identity source")
    output = args.output or ROOT / "data/ml/identity-review" / VERSION / mode
    staging = output / "evidence.sqlite"
    registry = args.registry or output / "registry.sqlite"
    state = extract(staging, source, pace=pace)
    if not state["dataset"]:
        sys.stdout.buffer.write(
            encoded({"complete": False, "scanned": state["scanned"], "pages": state["pages"]})
        )
        return 2
    workbook = output / "player-review.xlsx"
    if workbook.exists():
        # Immutable owner packets must not be silently overwritten.
        sys.stdout.buffer.write(
            encoded(
                {
                    "complete": True,
                    "dataset": state["dataset"],
                    "workbook": str(workbook),
                    "reused": True,
                }
            )
        )
    else:
        report = create_workbook(staging, registry, workbook)
        sys.stdout.buffer.write(encoded({"complete": True, "scanned": state["scanned"], **report}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
