"""Immutable, local Parquet snapshots with a bounded per-match build."""

import fcntl
import json
import os
import platform
import shutil
import tempfile
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from unicodedata import unidata_version

import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import MODES, SCHEMAS, VERSION
from .evidence import encoded, fingerprint, instant
from .source import SOURCES
from .transform import MatchTransform

SNAPSHOT_FORMAT = "content-addressed-v1"


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
    manifest_bytes = (folder / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    snapshot_format = manifest.get("snapshot_format")
    if snapshot_format is None:
        # Older snapshots retain their input-addressed IDs and are never rewritten.
        addressed = manifest["inputs"]
    elif snapshot_format == SNAPSHOT_FORMAT:
        if manifest_bytes != encoded(manifest):
            raise ValueError("Non-canonical snapshot manifest")
        addressed = {key: value for key, value in manifest.items() if key != "version"}
    else:
        raise ValueError("Unsupported snapshot format")
    if fingerprint(addressed) != version or manifest["version"] != version:
        raise ValueError("Snapshot version mismatch")
    if checksum(folder / "quality_report.json") != manifest["report_sha256"]:
        raise ValueError("Quality report checksum mismatch")
    for name, expected in manifest["outputs"].items():
        path = folder / f"{name}.parquet"
        if artifact(path) != expected:
            raise ValueError(f"Invalid Parquet artifact: {path}")
    if snapshot_format == SNAPSHOT_FORMAT:
        for mid, shard in manifest["shards"].items():
            for name, expected in shard["files"].items():
                path = folder / "shards" / mid / f"{name}.parquet"
                if artifact(path) != expected:
                    raise ValueError(f"Invalid Parquet artifact: {path}")
    return folder, manifest


def copy_shard(previous, destination):
    destination.mkdir(parents=True)
    for name in SCHEMAS:
        file = f"{name}.parquet"
        os.link(previous / file, destination / file)


def write_shard(path, bundle, config, identity_bindings=None):
    tables = MatchTransform(bundle, config, identity_bindings).run()
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
        "match_local_winners": sum(
            r.get("winner_side") is not None for r in tables["match_results"]
        ),
        "eligible_targets": sum(bool(r.get("target_eligible")) for r in tables["match_results"]),
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


def quality_report(config, shards, outputs):
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
    return {
        "transform_version": VERSION,
        "mode": config["mode"],
        "prediction_cutoff": config.get("prediction_cutoff"),
        "processed_matches": len(shards),
        "source_files": len(SOURCES) * len(shards),
        "source_rows": dict(source_rows),
        "rows": {name: item["rows"] for name, item in outputs.items()},
        "candidates": sum(info["candidates"] for info in shards.values()),
        "match_local_winners": sum(info["match_local_winners"] for info in shards.values()),
        "eligible_targets": sum(info["eligible_targets"] for info in shards.values()),
        "confirmed_results": sum(info["targets"] for info in shards.values()),
        "player_observations": dict(statuses),
        "quality": quality,
        "quality_by_source": by_source,
        "missing_checks": sum(group["missing"] for group in quality.values()),
        "conflicting_checks": sum(group["conflict"] for group in quality.values()),
        "restriction_reasons": dict(reasons),
    }


def run_report(quality, publication, changes, previous, shards):
    affected = set(changes["new"] + changes["changed"] + changes["removed"])
    dates, players = set(), set()
    for mid in affected:
        for entry in (previous.get(mid, {}), shards.get(mid, {})):
            if entry.get("match_date"):
                dates.add(entry["match_date"])
            players.update(entry.get("players", []))
    return {
        **quality,
        **publication,
        "changes": {key: len(value) for key, value in changes.items()},
        "invalidation": {
            "match_ids": sorted(affected),
            "player_ids": sorted(players),
            "dates": sorted(dates),
            "earliest_date": min(dates) if dates else None,
            "requires_future_opponent_replay": bool(affected),
        },
    }


def read_pointer(path):
    raw = path.read_text().strip()
    if raw.startswith("{"):
        return json.loads(raw)
    return {"version": raw, "published_at": None}


def update_pointer(root, version):
    publication = {"version": version, "published_at": datetime.now(UTC).isoformat()}
    with tempfile.NamedTemporaryFile(dir=root, prefix=".current-", delete=False) as stream:
        path = Path(stream.name)
        stream.write(encoded(publication))
    try:
        os.replace(path, root / "CURRENT")
    finally:
        path.unlink(missing_ok=True)
    return publication


def build(adapter, output, config, *, identities=None):
    if config.get("version") != VERSION or config.get("mode") not in MODES:
        raise ValueError("Unsupported canonical configuration")
    if config.get("prediction_cutoff") is not None and instant(config["prediction_cutoff"]) is None:
        raise ValueError("Prediction cutoff needs an explicit UTC offset")
    mode_root = Path(output) / config["mode"]
    mode_root.mkdir(parents=True, exist_ok=True)
    with (mode_root / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return build_locked(adapter, mode_root, config, identities)


def transform_code_hash():
    ml = Path(__file__).parent.parent
    sources = (
        *Path(__file__).parent.glob("*.py"),
        ml / "canonical_match.py",
        ml / "player_identity.py",
        ml.parent / "ingestion/nbbet/pbp_parsing.py",
    )
    return fingerprint({p.name: checksum(p) for p in sources})


def build_locked(adapter, root, config, identities):
    entries = dict(sorted(adapter.entries()))
    general = {k: v for k, v in config.items() if k != "reviews"}
    code_hash = transform_code_hash()
    runtime = {
        "python": platform.python_version(),
        "unicode": unidata_version,
        "pyarrow": pa.__version__,
        "arrow_cpp": pa.cpp_build_info.version,
        "arrow_package": pa.cpp_build_info.package_kind,
        "platform": [platform.system(), platform.machine()],
    }
    bindings = (
        {}
        if identities is None
        else {
            mid: identities.resolve(*adapter.match_metadata(mid, sha))
            for mid, sha in entries.items()
        }
    )
    inputs = {
        "registry_token": identities.token if identities else None,
        "runtime": runtime,
        "transform": VERSION,
        "code_sha256": code_hash,
        "config_sha256": fingerprint(config),
        "matches": {
            mid: {
                "manifest_sha256": sha,
                "transform_sha256": fingerprint(
                    [
                        code_hash,
                        runtime,
                        general,
                        config.get("reviews", {}).get(mid),
                        bindings.get(mid),
                    ]
                ),
            }
            for mid, sha in entries.items()
        },
    }
    pointer = root / "CURRENT"
    previous_folder = None
    previous = {}
    previous_inputs = {}
    current = read_pointer(pointer) if pointer.exists() else None
    if current:
        previous_folder, old = manifest_for(root, current["version"])
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
    if current and old.get("snapshot_format") == SNAPSHOT_FORMAT and old["inputs"] == inputs:
        for mid, sha in entries.items():
            adapter.verify(mid, sha)
        quality = json.loads((previous_folder / "quality_report.json").read_bytes())
        return run_report(quality, current, changes, previous, previous)
    stage = Path(tempfile.mkdtemp(dir=root, prefix=".stage-"))
    try:
        shards = {}
        unchanged = set(changes["skipped"])
        for mid, sha in entries.items():
            destination = stage / "shards" / mid
            if mid in unchanged:
                adapter.verify(mid, sha)
                copy_shard(previous_folder / "shards" / mid, destination)
                shards[mid] = previous[mid]
            else:
                shards[mid] = write_shard(
                    destination, adapter.load(mid, sha), config, bindings.get(mid)
                )
        outputs = write_outputs(stage, entries)
        quality = quality_report(config, shards, outputs)
        (stage / "quality_report.json").write_bytes(encoded(quality))
        manifest = {
            "snapshot_format": SNAPSHOT_FORMAT,
            "inputs": inputs,
            "outputs": outputs,
            "shards": shards,
            "report_sha256": checksum(stage / "quality_report.json"),
        }
        version = fingerprint(manifest)
        (stage / "manifest.json").write_bytes(encoded({**manifest, "version": version}))
        for name, schema in SCHEMAS.items():
            verify_parquet(stage / f"{name}.parquet", outputs[name], schema)
        target = root / "versions" / version
        if target.exists():
            manifest_for(root, version)
        else:
            stage.rename(target)
        publication = update_pointer(root, version)
        return run_report(quality, publication, changes, previous, shards)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
