"""Read a result-only projection of pinned local snapshots."""

from collections import Counter
from dataclasses import dataclass
from datetime import date

import pyarrow.parquet as pq

from tennis_predictor.ml.aggregation.batch import CanonicalReader
from tennis_predictor.ml.canonical_layer.storage import checksum

SURFACES = {"hard", "clay", "grass", "carpet"}


@dataclass(frozen=True)
class Match:
    match_id: str
    day: date
    gender: str
    level: str
    surface: str | None
    left: str
    right: str
    score: int
    target: bool
    revision: str


def result(sides, metrics):
    sides = sorted(sides, key=lambda row: row["side"])
    if len(sides) != 2 or [row["side"] for row in sides] != [1, 2]:
        return None
    if any(row["status"] != 3 or row["duplicate_of"] or not row["player_id"] for row in sides):
        return None
    if sides[0]["player_id"] == sides[1]["player_id"]:
        return None
    wins = [row for row in metrics if row["metric"] == "match_win" and row["scope"] == 0]
    if (
        len(wins) != 2
        or any(row["quality"] != "VALID" or not row["history_eligible"] for row in wins)
        or {row["side"] for row in wins} != {1, 2}
        or {row["numerator"] for row in wins} != {0, 1}
    ):
        return None
    return (
        sides[0]["player_id"],
        sides[1]["player_id"],
        next(row["numerator"] for row in wins if row["side"] == 1),
    )


def load(snapshot, canonical_root, registry):
    canonical = CanonicalReader(canonical_root, snapshot.manifest["canonical_snapshot"])
    binding = canonical.manifest["inputs"]
    if binding.get("registry_token") != registry.token:
        raise ValueError("Match Data Store must be built with the exact approved registry pin")
    day_counts = {
        pid: Counter(day for day, _, _ in rows)
        for pid, rows in snapshot.manifest["player_index"].items()
    }
    matches = []
    for mid, entry in sorted(snapshot.manifest["matches"].items()):
        if not entry["match_date"] or len(entry["players"]) != 2:
            continue
        tables = snapshot.match(mid)
        outcome = result(tables["match_sides"], tables["metrics"])
        if outcome is None:
            continue
        path = canonical.folder / "shards" / mid / "matches.parquet"
        expected = canonical.manifest["shards"][mid]["files"]["matches"]["sha256"]
        if checksum(path) != expected:
            raise ValueError(f"Corrupt canonical match metadata: {mid}")
        (meta,) = pq.ParquetFile(path).read().to_pylist()
        if meta["duplicate_of"] or meta["gender"] not in ("M", "F"):
            continue
        if meta["match_date"].isoformat() != entry["match_date"]:
            raise ValueError("Canonical and Match Data Store dates differ")
        left, right, score = outcome
        target = all(day_counts[pid][entry["match_date"]] == 1 for pid in (left, right))
        surface = meta["surface"] if meta["surface"] in SURFACES else None
        matches.append(
            Match(
                mid,
                meta["match_date"],
                meta["gender"],
                meta["level"] or "unknown",
                surface,
                left,
                right,
                score,
                target,
                entry["revision"],
            )
        )
    return matches, {
        "match_data": snapshot.version,
        "canonical": canonical.version,
        "registry_revision": registry.revision,
        "registry_token": registry.token,
        "input_matches": len(snapshot.manifest["matches"]),
    }
