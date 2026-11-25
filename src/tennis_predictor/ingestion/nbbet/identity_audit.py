"""Offline, read-only discovery of candidate player identifiers.

Pass explicitly selected local details Parquet files. Never emit field values.
Candidate paths are structural evidence, not confirmed source identifiers.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path


def audit(rows, limit=10000):
    paths = Counter()
    values = defaultdict(Counter)
    counts = Counter()

    def walk(obj, path=(), depth=0):
        if depth > 4:
            return
        if isinstance(obj, dict):
            for key, value in list(obj.items())[:64]:
                # NBBet numeric schema keys only; do not expose arbitrary keys.
                if isinstance(key, str) and key.isascii() and key.isdecimal() and len(key) <= 6:
                    yield from walk(value, path + (key,), depth + 1)
        elif isinstance(obj, (str, int)) and not isinstance(obj, bool):
            if isinstance(obj, str) and (not obj or len(obj) > 128):
                return
            yield ".".join(path), (type(obj).__name__, obj)

    for row in rows:
        if counts["rows"] >= limit:
            break
        counts["rows"] += 1
        raw = row.get("players_json")
        if not isinstance(raw, str) or len(raw) > 65536:
            counts["missing_or_oversized"] += 1
            continue
        try:
            players = json.loads(raw)
        except (ValueError, RecursionError):
            counts["invalid_json"] += 1
            continue
        for side in ("p1", "p2"):
            player = players.get(side) if isinstance(players, dict) else None
            if not isinstance(player, dict):
                counts["missing_player"] += 1
                continue
            counts["player_objects"] += 1
            for path, value in walk(player):
                paths[path] += 1
                values[path][value] += 1
    return {"counts": dict(counts), "candidate_paths": [
        {"path": path, "observations": n, "distinct": len(values[path]),
         "repeated_values": sum(c > 1 for c in values[path].values())}
        for path, n in sorted(paths.items())
    ]}


def main():
    import pyarrow.parquet as pq

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000 or len(args.files) > 32:
        parser.error("limit must be 1..10000; at most 32 files")

    def rows():
        for path in args.files:
            if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("Expected local Parquet file <=64 MiB")
            parquet = pq.ParquetFile(path)
            for batch in parquet.iter_batches(batch_size=256, columns=["players_json"]):
                yield from batch.to_pylist()

    print(json.dumps(audit(rows(), args.limit), indent=2))


if __name__ == "__main__":
    main()
