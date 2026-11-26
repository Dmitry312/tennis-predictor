"""Immutable, local Parquet snapshots with a bounded per-match build."""

import fcntl
import json
import os
import shutil
import tempfile
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import MODES, SCHEMAS, VERSION
from .evidence import encoded, fingerprint, instant
from .source import SOURCES
from .transform import MatchTransform


def checksum(path):
    with path.open("rb") as stream:
        hasher = sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
        return hasher.hexdigest()


def artifact(path):
    return {"sha256": checksum(path), "rows": pq.ParquetFile(path).metadata.num_rows}


def verify_parquet(path, expected, schema):
    if artifact(path) != expected or not pq.read_schema(path).equals(schema, check_metadata=True):
        raise ValueError(f"Invalid Parquet artifact: {path}")


def manifest_for(root, version):
    if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
        raise ValueError("Invalid snapshot pointer")
    folder = root / "versions" / version
    manifest = json.loads((folder / "manifest.json").read_bytes())
    if fingerprint(manifest["inputs"]) != version or manifest["version"] != version:
        raise ValueError("Snapshot version mismatch")
    if checksum(folder / "quality_report.json") != manifest["report_sha256"]:
        raise ValueError("Quality report checksum mismatch")
    for name, expected in manifest["outputs"].items():
        path = folder / f"{name}.parquet"
        if artifact(path) != expected:
            raise ValueError(f"Invalid Parquet artifact: {path}")
    return folder, manifest


def copy_shard(previous, destination, metadata):
    destination.mkdir(parents=True)
    for name, schema in SCHEMAS.items():
        file = f"{name}.parquet"
        verify_parquet(previous / file, metadata["files"][name], schema)
        os.link(previous / file, destination / file)


def write_shard(path, bundle, config, recorded_at):
    tables = MatchTransform(bundle, config, recorded_at).run()
    path.mkdir(parents=True)
    files = {}
    for name, schema in SCHEMAS.items():
        filename = path / f"{name}.parquet"
        table = pa.Table.from_pylist(tables[name], schema=schema)
        table.validate(full=True)
        pq.write_table(table, filename, compression="zstd")
        files[name] = artifact(filename)
    match = tables["matches"][0]
    checks = tables["data_quality"]
    source_rows = Counter()
    for observation in tables["data_manifest"]:
        source_rows[observation["source"]] += observation["input_rows"]
    return {
        "source_rows": dict(source_rows),
        "files": files,
        "players": sorted({r["player_id"] for r in tables["players"]}),
        "match_date": match["match_date"].isoformat() if match["match_date"] else None,
        "candidates": sum(
            r.get("source_candidate_winner_side") is not None for r in tables["match_results"]
        ),
        "targets": sum(r.get("target_p1_win") is not None for r in tables["match_results"]),
        "player_statuses": dict(Counter(r["resolution_status"] for r in tables["players"])),
        "quality": [
            [*key, n]
            for key, n in sorted(
                Counter(
                    (r["source"], r["category"], r["status"], r["reason"]) for r in checks
                ).items()
            )
        ],
    }


def write_outputs(stage, match_ids):
    files = {}
    for name, schema in SCHEMAS.items():
        filename = stage / f"{name}.parquet"
        with pq.ParquetWriter(filename, schema, compression="zstd") as writer:
            for mid in match_ids:
                source = pq.ParquetFile(stage / "shards" / mid / f"{name}.parquet")
                for batch in source.iter_batches(batch_size=8192):
                    writer.write_batch(batch)
        files[name] = artifact(filename)
    return files


def report_for(version, config, shards, outputs, changes, previous):
    affected = set(changes["new"] + changes["changed"] + changes["removed"])
    dates, players = set(), set()
    for mid in affected:
        for entry in (previous.get(mid, {}), shards.get(mid, {})):
            if entry.get("match_date"):
                dates.add(entry["match_date"])
            players.update(entry.get("players", []))
    quality, by_source = {}, {}
    reasons, statuses, source_rows = Counter(), Counter(), Counter()
    for info in shards.values():
        statuses.update(info["player_statuses"])
        source_rows.update(info["source_rows"])
        for source, category, status, reason, count in info["quality"]:
            quality.setdefault(category, Counter())[status] += count
            by_source.setdefault(source, {}).setdefault(category, Counter())[status] += count
            if status != "valid":
                reasons[reason] += count
    change_counts = {key: len(value) for key, value in changes.items()}
    return {
        "version": version,
        "transform_version": VERSION,
        "mode": config["mode"],
        "prediction_cutoff": config.get("prediction_cutoff"),
        "processed_matches": len(shards),
        "changes": change_counts,
        "source_files": len(SOURCES) * len(shards),
        "source_rows": dict(source_rows),
        "rows": {name: item["rows"] for name, item in outputs.items()},
        "candidates": sum(info["candidates"] for info in shards.values()),
        "confirmed_results": sum(info["targets"] for info in shards.values()),
        "player_observations": dict(statuses),
        "quality": quality,
        "quality_by_source": by_source,
        "missing_checks": sum(group["missing"] for group in quality.values()),
        "conflicting_checks": sum(group["conflict"] for group in quality.values()),
        "restriction_reasons": dict(reasons),
        "invalidation": {
            "match_ids": sorted(affected),
            "player_ids": sorted(players),
            "dates": sorted(dates),
            "earliest_date": min(dates) if dates else None,
            "requires_future_opponent_replay": bool(affected),
        },
    }


def update_pointer(root, version):
    with tempfile.NamedTemporaryFile(dir=root, prefix=".current-", delete=False) as stream:
        path = Path(stream.name)
        stream.write((version + "\n").encode())
    try:
        os.replace(path, root / "CURRENT")
    finally:
        path.unlink(missing_ok=True)


def build(adapter, output, config):
    if config.get("version") != VERSION or config.get("mode") not in MODES:
        raise ValueError("Unsupported canonical configuration")
    if config.get("prediction_cutoff") is not None and instant(config["prediction_cutoff"]) is None:
        raise ValueError("Prediction cutoff needs an explicit UTC offset")
    mode_root = Path(output) / config["mode"]
    mode_root.mkdir(parents=True, exist_ok=True)
    with (mode_root / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return build_locked(adapter, mode_root, config)


def build_locked(adapter, root, config):
    entries = dict(adapter.entries())
    general = {k: v for k, v in config.items() if k != "reviews"}
    ml = Path(__file__).parent.parent
    sources = (
        *Path(__file__).parent.glob("*.py"),
        ml / "canonical_match.py",
        ml / "player_identity.py",
        ml.parent / "ingestion/nbbet/pbp_parsing.py",
    )
    code_hash = fingerprint({p.name: checksum(p) for p in sources})
    inputs = {
        "transform": VERSION,
        "code_sha256": code_hash,
        "config_sha256": fingerprint(config),
        "matches": {
            mid: {
                "manifest_sha256": sha,
                "transform_sha256": fingerprint(
                    [code_hash, general, config.get("reviews", {}).get(mid)]
                ),
            }
            for mid, sha in entries.items()
        },
    }
    version = fingerprint(inputs)
    pointer = root / "CURRENT"
    previous_folder = None
    previous = {}
    previous_inputs = {}
    if pointer.exists():
        previous_folder, old = manifest_for(root, pointer.read_text().strip())
        previous = old["shards"]
        previous_inputs = old["inputs"]["matches"]
    changes = {
        "new": sorted(set(entries) - set(previous)),
        "changed": sorted(
            mid
            for mid in entries.keys() & previous.keys()
            if inputs["matches"][mid] != previous_inputs[mid]
        ),
        "skipped": sorted(
            mid
            for mid in entries.keys() & previous.keys()
            if inputs["matches"][mid] == previous_inputs[mid]
        ),
        "removed": sorted(set(previous) - set(entries)),
    }
    root.joinpath("versions").mkdir(exist_ok=True)
    target = root / "versions" / version
    if target.exists():
        _, existing = manifest_for(root, version)
        for mid, sha in entries.items():
            # Check local sample bytes even when the index promises unchanged content.
            adapter.verify(mid, sha)
        report = report_for(
            version, config, existing["shards"], existing["outputs"], changes, previous
        )
        update_pointer(root, version)
        return report
    stage = Path(tempfile.mkdtemp(dir=root, prefix=".stage-"))
    try:
        recorded = datetime.now(UTC)
        shards = {}
        unchanged = set(changes["skipped"])
        for mid, sha in entries.items():
            destination = stage / "shards" / mid
            if mid in unchanged:
                adapter.verify(mid, sha)
                copy_shard(previous_folder / "shards" / mid, destination, previous[mid])
                shards[mid] = previous[mid]
            else:
                shards[mid] = write_shard(destination, adapter.load(mid, sha), config, recorded)
        outputs = write_outputs(stage, entries)
        report = report_for(version, config, shards, outputs, changes, previous)
        (stage / "quality_report.json").write_bytes(encoded(report))
        (stage / "manifest.json").write_bytes(
            encoded(
                {
                    "version": version,
                    "inputs": inputs,
                    "outputs": outputs,
                    "shards": shards,
                    "recorded_at": recorded.isoformat(),
                    "report_sha256": checksum(stage / "quality_report.json"),
                }
            )
        )
        for name, schema in SCHEMAS.items():
            verify_parquet(stage / f"{name}.parquet", outputs[name], schema)
        stage.rename(target)
        update_pointer(root, version)
        return report
    finally:
        shutil.rmtree(stage, ignore_errors=True)
