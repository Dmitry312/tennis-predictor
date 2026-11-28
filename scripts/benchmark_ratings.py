"""Benchmark rating replay with a generated local result stream."""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tennis_predictor.ml.ratings.engine import build
from tennis_predictor.ml.ratings.inputs import Match


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches", type=int, default=176_000)
    args = parser.parse_args()
    if args.matches < 200:
        parser.error("At least two synthetic periods are required")
    start = date(2016, 1, 1)
    surfaces = ("hard", "clay", "grass", None)
    matches = [
        Match(
            f"synthetic-{i:06d}",
            start + timedelta(days=i // 100),
            "M" if i % 2 else "F",
            "tour" if i % 3 else "challenger",
            surfaces[(i // 2) % 4],
            f"p{i * 17 % 4000}",
            f"p{(i * 29 + 1) % 4000}",
            (i * 7 % 11) > 4,
            True,
            f"synthetic-{i:06d}",
        )
        for i in range(args.matches)
    ]
    source = {"fixture": "synthetic-v1"}
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        start_time = perf_counter()
        full = build(root / "full", matches, source)
        full["seconds"] = round(perf_counter() - start_time, 3)
        build(root / "incremental", matches[:-100], source)
        start_time = perf_counter()
        incremental = build(root / "incremental", matches, source)
        incremental["seconds"] = round(perf_counter() - start_time, 3)
        assert full["version"] == incremental["version"]
        print(json.dumps({"full": full, "append": incremental}, indent=2))


if __name__ == "__main__":
    main()
