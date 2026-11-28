"""Build and read immutable local snapshots pinned to a canonical snapshot."""

import fcntl
import json
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from uuid import UUID

import pyarrow.parquet as pq

from tennis_predictor.ml.canonical_layer.evidence import digest, encoded, fingerprint
from tennis_predictor.ml.canonical_layer.storage import checksum, manifest_for, read_pointer

from .contracts import SCHEMAS, VERSION, MatchInput, historical_before, utc_time
from .store import MatchDataStore, unpack

INPUT_TABLES = (
    "matches",
    "players",
    "player_aliases",
    "match_results",
    "match_player_stats",
    "match_player_pbp",
    "data_quality",
)


class CanonicalReader:
    def __init__(self, mode_root, version=None):
        mode_root = Path(mode_root)
        version = version or read_pointer(mode_root / "CURRENT")["version"]
        self.folder, self.manifest = manifest_for(mode_root, version)
        self.version = version

    def entries(self):
        for mid, shard in sorted(self.manifest["shards"].items()):
            if str(UUID(mid)) != mid:
                raise ValueError("Invalid canonical match ID")
            # Include only tables actually used by this engine.
            yield mid, fingerprint({name: shard["files"][name]["sha256"] for name in INPUT_TABLES})

    def load(self, mid, revision):
        folder = self.folder / "shards" / mid
        tables = {}
        for name in INPUT_TABLES:
            path = folder / f"{name}.parquet"
            if checksum(path) != self.manifest["shards"][mid]["files"][name]["sha256"]:
                raise ValueError(f"Canonical source changed during build: {path}")
            tables[name] = pq.ParquetFile(path).read().to_pylist()
        return MatchInput(mid, revision, tables)


def match_report(sides, metrics):
    lines = []
    by_side = defaultdict(list)
    for row in metrics:
        by_side[row["side"]].append(row)
    for side in sides:
        key = side["side"]
        lines.append(
            f"{side['match_id']} | side {key} | {side['display_name']} | {side['match_date']}"
        )
        for row in by_side[key]:
            value = row["numerator"]
            if row["denominator"] is not None:
                value = f"{value}/{row['denominator']}"
            if row["raw_value"] is not None:
                value = f"{value} (raw {row['raw_value']})"
            refs = ", ".join(r["evidence_ref"] for r in row["source_refs"] if r["evidence_ref"])
            lines.append(
                f"  {row['family']}/{row['metric']} scope={row['scope']} game={row['game']} "
                f"{row['quality']} {value} [{row['reason']}] {refs}"
            )
    return "\n".join(lines)


class SnapshotReader:
    def __init__(self, output, version=None):
        output = Path(output)
        self.version = version or (output / "CURRENT").read_text().strip()
        if len(self.version) != 64 or any(c not in "0123456789abcdef" for c in self.version):
            raise ValueError("Invalid aggregation snapshot ID")
        self.folder = output / "versions" / self.version
        raw = (self.folder / "manifest.json").read_bytes()
        self.manifest = json.loads(raw)
        if raw != encoded(self.manifest) or fingerprint(self.manifest) != self.version:
            raise ValueError("Invalid aggregation manifest")
        if self.manifest["contract"] != VERSION:
            raise ValueError("Unsupported aggregation contract")

    def match(self, match_id):
        entry = self.manifest["matches"][match_id]
        folder = self.folder / "shards" / match_id
        tables = {}
        for name, expected in entry["files"].items():
            payload = (folder / f"{name}.parquet").read_bytes()
            if digest(payload) != expected:
                raise ValueError(f"Corrupt aggregation shard: {match_id}/{name}")
            tables[name] = unpack(payload)
        return tables

    def history(self, player_id, *, prediction_date, observed_cutoff=None):
        """History for an already verified ID, within this exact snapshot."""
        cutoff = utc_time(observed_cutoff) if observed_cutoff is not None else None
        if cutoff is not None:
            prediction_date = min(prediction_date, cutoff.date())
        for day, match_id, side in self.manifest["player_index"].get(player_id, []):
            if historical_before(date.fromisoformat(day), prediction_date):
                for row in self.match(match_id)["metrics"]:
                    if row["side"] != side or not row["history_eligible"]:
                        continue
                    if cutoff is not None:
                        bounds = (row["available_at"], row["identity_recorded_at"])
                        if any(value is None or value >= cutoff for value in bounds):
                            continue
                    yield {
                        **row,
                        "revision": self.manifest["matches"][match_id]["revision"],
                        "availability_policy": "strict_observed" if cutoff else "historical_date",
                    }

    def recent_feature_facts(self, player_id, prediction_date, observed_cutoff=None):
        from tennis_predictor.ml.player_features.engine import observations

        cutoff = utc_time(observed_cutoff) if observed_cutoff is not None else None
        if cutoff is not None:
            prediction_date = min(prediction_date, cutoff.date())
        count = 0
        for day, mid, _ in reversed(self.manifest["player_index"].get(player_id, [])):
            parsed = date.fromisoformat(day)
            if parsed >= prediction_date:
                continue
            rows = self.match(mid)["metrics"]
            if cutoff is not None:
                rows = [
                    row
                    for row in rows
                    if row["identity_recorded_at"] is not None
                    and row["identity_recorded_at"] < cutoff
                    and row["available_at"] is not None
                    and row["available_at"] < cutoff
                ]
            record = observations(
                rows, player_id, parsed, self.manifest["matches"][mid]["revision"], mid
            )
            if record is not None:
                yield record
                count += 1
                if count == 15:
                    break

    def unique_target_day(self, player_id, prediction_date):
        """Future evaluation gate. Facts on a double-match day remain intact."""
        matches = {
            mid
            for day, mid, _ in self.manifest["player_index"].get(player_id, [])
            if day == prediction_date.isoformat()
        }
        return len(matches) == 1


def build(adapter, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return build_locked(adapter, output)


def build_locked(adapter, output):
    store = MatchDataStore(output / "match-store.sqlite")
    current = output / "CURRENT"
    previous = SnapshotReader(output).manifest["matches"] if current.exists() else {}
    versions = output / "versions"
    versions.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=output, prefix=".stage-"))
    entries = {}
    processed = reused = 0
    totals, by_metric = Counter(), defaultdict(Counter)
    player_index = defaultdict(list)
    try:
        with (stage / "matches.txt").open("w", encoding="utf-8") as report:
            for mid, inp in sorted(adapter.entries()):
                if str(UUID(mid)) != mid or mid in entries:
                    raise ValueError("Invalid or duplicate match ID")
                revision = store.cached(mid, inp)
                if revision is None:
                    revision = store.process(adapter.load(mid, inp))
                    processed += 1
                else:
                    reused += 1
                files = store.files(revision)
                folder = stage / "shards" / mid
                folder.mkdir(parents=True)
                for name, payload in files.items():
                    (folder / f"{name}.parquet").write_bytes(payload)
                sides, metrics = (unpack(files[name]) for name in SCHEMAS)
                entries[mid] = {
                    "input_revision": inp,
                    "revision": revision,
                    "files": {name: digest(payload) for name, payload in files.items()},
                    "match_date": sides[0]["match_date"].isoformat()
                    if sides[0]["match_date"]
                    else None,
                    "players": sorted({r["player_id"] for r in sides if r["player_id"]}),
                }
                for row in sides:
                    if row["player_id"] and row["match_date"]:
                        player_index[row["player_id"]].append(
                            [
                                row["match_date"].isoformat(),
                                mid,
                                row["side"],
                            ]
                        )
                totals.update(r["quality"] for r in metrics)
                for row in metrics:
                    by_metric[row["metric"]][row["quality"]] += 1
                report.write(match_report(sides, metrics) + "\n\n")
        summary = {
            "matches": len(entries),
            "match_sides": len(entries) * 2,
            "metric_observations": sum(totals.values()),
            "quality": totals,
            "by_metric": by_metric,
        }
        (stage / "quality_report.json").write_bytes(encoded(summary))
        manifest = {
            "contract": VERSION,
            "canonical_snapshot": adapter.version,
            "engine": store.engine,
            "matches": entries,
            "player_index": {k: sorted(v) for k, v in player_index.items()},
            "reports": {
                name: checksum(stage / name) for name in ("matches.txt", "quality_report.json")
            },
        }
        version = fingerprint(manifest)
        (stage / "manifest.json").write_bytes(encoded(manifest))
        target = versions / version
        if target.exists():
            reader = SnapshotReader(output, version)
            for name, sha in reader.manifest["reports"].items():
                if checksum(target / name) != sha:
                    raise ValueError("Corrupt aggregation report")
            for mid in entries:
                reader.match(mid)
        else:
            stage.rename(target)
        with tempfile.NamedTemporaryFile(dir=output, prefix=".current-", delete=False) as pointer:
            pointer_path = Path(pointer.name)
            pointer.write((version + "\n").encode())
        try:
            pointer_path.replace(current)
        finally:
            pointer_path.unlink(missing_ok=True)
        invalidated = sorted(
            mid for mid in entries.keys() | previous.keys() if entries.get(mid) != previous.get(mid)
        )
        impacted = [
            entry
            for mid in invalidated
            for entry in (entries.get(mid), previous.get(mid))
            if entry is not None
        ]
        return {
            **summary,
            "version": version,
            "canonical_snapshot": adapter.version,
            "processed": processed,
            "reused": reused,
            "invalidated": invalidated,
            "invalidated_player_ids": sorted(
                {pid for entry in impacted for pid in entry["players"]}
            ),
            "earliest_affected_date": min(
                (entry["match_date"] for entry in impacted if entry["match_date"]), default=None
            ),
        }
    finally:
        shutil.rmtree(stage, ignore_errors=True)
