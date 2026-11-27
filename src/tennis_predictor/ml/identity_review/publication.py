"""Explicit versioned registry publication using the existing S3 client."""

import json
import os
import re
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from botocore.exceptions import ClientError

from tennis_predictor.ml.canonical_layer.evidence import digest, encoded
from tennis_predictor.ml.canonical_layer.storage import checksum

MAX_REGISTRY = 128 * 1024 * 1024
MAX_JSON = 16 * 1024
FORMAT = "player-registry-s3-v1"


def hex64(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def read_registry(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Registry SQLite integrity failure")
        row = db.execute(
            "SELECT revision,token FROM revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if not row or type(row[0]) is not int or row[0] < 1 or not hex64(row[1]):
            raise ValueError("Registry has no published review revision")
        return row


def snapshot(source, destination):
    with (
        closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(destination)) as dst,
    ):
        src.backup(dst)
    os.chmod(destination, 0o600)
    revision, token = read_registry(destination)
    size = destination.stat().st_size
    if not 0 < size <= MAX_REGISTRY:
        raise ValueError("Registry snapshot byte limit")
    return {
        "format": FORMAT,
        "revision": revision,
        "token": token,
        "size": size,
        "sha256": checksum(destination),
    }


def absent(error):
    return error.response["Error"]["Code"] in ("NoSuchKey", "NotFound", "404")


def precondition(error):
    return error.response["Error"]["Code"] in ("PreconditionFailed", "412")


class RegistryPublication:
    def __init__(self, client, bucket, prefix="ml/player-registry/v1"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
            raise ValueError("Invalid registry prefix")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def get(self, path):
        return self.client.get_object(Bucket=self.bucket, Key=f"{self.prefix}/{path}")

    def put(self, path, body, **conditions):
        self.client.put_object(
            Bucket=self.bucket, Key=f"{self.prefix}/{path}", Body=body, **conditions
        )

    def metadata(self, path):
        response = self.get(path)
        with closing(response["Body"]) as stream:
            raw = stream.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError("Oversize registry metadata")
        data = json.loads(raw)
        if not isinstance(data, dict) or encoded(data) != raw:
            raise ValueError("Invalid registry metadata")
        return data, raw, response.get("ETag")

    def current(self):
        try:
            pointer, _, etag = self.metadata("CURRENT.json")
        except ClientError as exc:
            if absent(exc):
                return None, None
            raise
        if (
            set(pointer) != {"token", "manifest_sha256"}
            or not hex64(pointer["token"])
            or not hex64(pointer["manifest_sha256"])
            or not etag
        ):
            raise ValueError("Invalid registry pointer")
        return pointer, etag

    def manifest(self, token):
        if not hex64(token):
            raise ValueError("Invalid registry token")
        manifest, raw, _ = self.metadata(f"versions/{token}.json")
        if (
            set(manifest) != {"format", "revision", "token", "size", "sha256"}
            or manifest["format"] != FORMAT
            or manifest["token"] != token
            or not hex64(manifest["sha256"])
            or type(manifest["revision"]) is not int
            or manifest["revision"] < 1
            or type(manifest["size"]) is not int
            or not 0 < manifest["size"] <= MAX_REGISTRY
        ):
            raise ValueError("Invalid immutable registry manifest")
        return manifest, raw

    def copy_verified(self, manifest, output):
        response = self.get(f"snapshots/{manifest['sha256']}.sqlite")
        with closing(response["Body"]) as stream, output.open("xb") as destination:
            os.chmod(output, 0o600)
            copied = 0
            hasher = sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                copied += len(block)
                if copied > manifest["size"]:
                    raise ValueError("Registry object exceeds manifest size")
                hasher.update(block)
                destination.write(block)
            if copied != manifest["size"] or hasher.hexdigest() != manifest["sha256"]:
                raise ValueError("Registry object integrity failure")
            destination.flush()
            os.fsync(destination.fileno())
        if read_registry(output) != (manifest["revision"], manifest["token"]):
            raise ValueError("Registry revision does not match its manifest")

    def fetch(self, destination, token=None):
        """Verify a pinned/current snapshot and replace only the local cache."""
        pointer = None
        if token is None:
            pointer, _ = self.current()
            if pointer is None:
                raise ValueError("No published registry")
            token = pointer["token"]
        manifest, raw = self.manifest(token)
        if pointer and digest(raw) != pointer["manifest_sha256"]:
            raise ValueError("Registry pointer manifest digest mismatch")
        destination = Path(destination)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TemporaryDirectory(dir=destination.parent) as directory:
            temporary = Path(directory) / "registry.sqlite"
            self.copy_verified(manifest, temporary)
            if any(
                Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
            ):
                raise ValueError("Destination must be an immutable local registry cache")
            os.replace(temporary, destination)
        return manifest

    @staticmethod
    def require_previous(pointer, expected):
        previous = pointer["token"] if pointer else "root"
        if previous != expected:
            raise ValueError("Registry CURRENT does not match the expected predecessor")

    def put_immutable(self, path, body):
        try:
            self.put(path, body, IfNoneMatch="*")
        except ClientError as exc:
            if not precondition(exc):
                raise

    def activate(self, manifest, raw, etag):
        pointer = {"token": manifest["token"], "manifest_sha256": digest(raw)}
        condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        self.put("CURRENT.json", encoded(pointer), **condition)

    def publish(self, source, *, expected_current):
        """Write immutable artifacts, verify them, then CAS the pointer."""
        pointer, etag = self.current()
        with TemporaryDirectory() as directory:
            local = Path(directory) / "local.sqlite"
            manifest = snapshot(source, local)
            raw = encoded(manifest)
            target = {"token": manifest["token"], "manifest_sha256": digest(raw)}
            if pointer == target:
                current, current_raw = self.manifest(pointer["token"])
                if current_raw != raw:
                    raise ValueError("Registry pointer manifest digest mismatch")
                self.copy_verified(current, Path(directory) / "verified.sqlite")
                return manifest
            self.require_previous(pointer, expected_current)
            if pointer:
                current, current_raw = self.manifest(pointer["token"])
                if digest(current_raw) != pointer["manifest_sha256"]:
                    raise ValueError("Registry pointer manifest digest mismatch")
                with closing(
                    sqlite3.connect(local.resolve().as_uri() + "?mode=ro", uri=True)
                ) as db:
                    predecessor = db.execute(
                        "SELECT revision FROM revisions WHERE token=?", (pointer["token"],)
                    ).fetchone()
                if (
                    predecessor != (current["revision"],)
                    or manifest["revision"] <= current["revision"]
                ):
                    raise ValueError("Publish requires a descendant of the active registry")
            with local.open("rb") as stream:
                self.put_immutable(f"snapshots/{manifest['sha256']}.sqlite", stream)
            self.put_immutable(f"versions/{manifest['token']}.json", raw)
            actual, actual_raw = self.manifest(manifest["token"])
            if actual_raw != raw:
                raise ValueError("Registry version already binds a different snapshot")
            self.copy_verified(actual, Path(directory) / "verified.sqlite")
            self.activate(actual, actual_raw, etag)
            return actual

    def rollback(self, token, *, expected_current):
        """Repoint to a verified earlier snapshot while preserving all versions."""
        pointer, etag = self.current()
        self.require_previous(pointer, expected_current)
        if pointer is None:
            raise ValueError("No published registry to roll back")
        current, current_raw = self.manifest(pointer["token"])
        if digest(current_raw) != pointer["manifest_sha256"]:
            raise ValueError("Registry pointer manifest digest mismatch")
        previous, raw = self.manifest(token)
        if previous["revision"] >= current["revision"]:
            raise ValueError("Rollback requires an earlier registry revision")
        with TemporaryDirectory() as directory:
            self.copy_verified(previous, Path(directory) / "verified.sqlite")
        self.activate(previous, raw, etag)
        return previous
