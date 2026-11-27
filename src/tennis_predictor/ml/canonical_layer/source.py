"""Read and verify one repository catalog bundle at a time."""

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .evidence import digest, fingerprint, instant

SOURCES = ("matches", "nbbet_details", "nbbet_raw", "nbbet_stats", "pbp")
MAX_FILE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Observation:
    source: str
    reference: str
    file_hash: str
    version: str
    metadata: dict
    rows: list[dict]
    row_indices: tuple[int, ...] = ()

    @property
    def coverage(self):
        return self.metadata.get("coverage", "unknown")


@dataclass(frozen=True)
class Bundle:
    match_id: str
    manifest: dict
    observations: tuple[Observation, ...]


def read(path: Path, expected=None, *, decode=True):
    with path.open("rb") as stream:
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"Catalog file exceeds local limit: {path}")
    actual = digest(content)
    if expected is not None and actual != expected:
        raise ValueError(f"Catalog checksum mismatch: {path}")
    return (json.loads(content) if decode else None), actual


def source_observations(source, data, path, file_hash):
    observations = []
    versions = [data] if source == "matches" else data["versions"]
    version_map = {}
    for index, version in enumerate(versions or [{"coverage": data["coverage"], "rows": []}]):
        metadata = {k: v for k, v in version.items() if k != "rows"}
        rows = version["rows"]
        reference = path
        if source != "matches" and versions:
            reference += f"#/versions/{index}"
        version_key = fingerprint(
            {
                "source": source,
                "object_key": metadata.get("object_key"),
                "object_version": metadata.get("version_id"),
                "etag": metadata.get("etag"),
                "object_sha256": metadata.get("content_sha256"),
                "schema": metadata.get("schema"),
                "fallback": fingerprint(version) if not metadata.get("content_sha256") else None,
            }
        )
        if version_key in version_map:
            if version_map[version_key] != rows:
                raise ValueError(f"Divergent rows for the same source object: {reference}")
            continue
        version_map[version_key] = rows
        # A rewritten object can retain historical rows written at different receipts.
        groups = defaultdict(list)
        for row_index, row in enumerate(rows):
            timestamp = (
                instant(row.get("fetched_at"), naive_utc=True)
                if source in {"nbbet_details", "nbbet_raw", "nbbet_stats"}
                else None
            )
            groups[timestamp].append((row_index, row))
        for timestamp, group in (groups or {None: []}).items():
            indices = tuple(i for i, _ in group)
            receipt = timestamp.isoformat() if timestamp else None
            observations.append(
                Observation(
                    source,
                    reference,
                    file_hash,
                    fingerprint([version_key, receipt]),
                    {**metadata, "row_receipt": receipt},
                    [row for _, row in group],
                    indices,
                )
            )
    return observations


class CatalogAdapter:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def entries(self):
        index, _ = read(self.root / "index.json")
        entries = index["real_examples"]
        seen = set()
        for entry in sorted(entries, key=lambda item: item["match_id"]):
            mid = entry["match_id"]
            if str(UUID(mid)) != mid or mid in seen or entry["path"] != f"samples/match_{mid}":
                raise ValueError(f"Invalid catalog index entry: {mid}")
            seen.add(mid)
            yield mid, entry["manifest_sha256"]

    def manifest(self, mid, expected):
        prefix = f"samples/match_{mid}"
        directory = self.root / prefix
        manifest, _ = read(directory / "manifest.json", expected)
        envelope = {k: v for k, v in manifest.items() if k != "bundle_digest"}
        if (
            manifest["match_id"] != mid
            or manifest["format"] != "real-match-samples-v1"
            or manifest["bundle_digest"] != fingerprint(envelope)
        ):
            raise ValueError(f"Invalid bundle manifest: {mid}")
        return manifest, directory

    def verify(self, mid, expected):
        manifest, directory = self.manifest(mid, expected)
        for source in SOURCES:
            filename = source + ".json"
            read(directory / filename, manifest["files"][filename], decode=False)

    def match_metadata(self, mid, expected):
        manifest, directory = self.manifest(mid, expected)
        data, _ = read(directory / "matches.json", manifest["files"]["matches.json"])
        if data["match_id"] != mid or len(data["rows"]) != 1 or data["rows"][0]["match_id"] != mid:
            raise ValueError("Invalid match metadata for registry resolution")
        return data["rows"][0], f"samples/match_{mid}/matches.json#/rows/0"

    def load(self, mid, expected):
        manifest, directory = self.manifest(mid, expected)
        prefix = f"samples/match_{mid}"
        observations = []
        for source in SOURCES:
            filename = source + ".json"
            data, file_hash = read(directory / filename, manifest["files"][filename])
            if data["match_id"] != mid or (source != "matches" and data["source"] != source):
                raise ValueError(f"Invalid source envelope: {prefix}/{filename}")
            observations.extend(
                source_observations(source, data, f"{prefix}/{filename}", file_hash)
            )
        return Bundle(mid, manifest, tuple(observations))


def compatible_versions(left, right, by_source, match_id, reviewed=()):
    if any(left.version in group and right.version in group for group in reviewed):
        return True
    # detail_job.flush stamps these siblings with one exact receipt time per write.
    # Receipt cannot link PBP or two revisions within the same source family member.
    siblings = {"nbbet_details", "nbbet_stats", "nbbet_raw"}
    if left.source == right.source or {left.source, right.source} - siblings:
        return False

    def receipt(obs):
        values = {
            instant(row.get("fetched_at"), naive_utc=True)
            for row in obs.rows
            if row.get("canonical_match_id") == match_id
        }
        return next(iter(values)) if len(values) == 1 else None

    timestamp = receipt(left)
    if timestamp is None or receipt(right) != timestamp:
        return False
    # Shared receipts cease to be unambiguous if a source has multiple such objects.
    return all(
        sum(receipt(candidate) == timestamp for candidate in by_source[obs.source]) == 1
        for obs in (left, right)
    )
