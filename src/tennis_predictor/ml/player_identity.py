"""Offline, append-only canonical player registry. Name evidence is never verified.

Snapshots contain private aliases/provenance. Do not log or upload real snapshots.
No network, database, application configuration, fuzzy matching or automatic merges.
"""
import copy
import hashlib
import json
import os
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = "player-registry-v1"
NORMALIZATION_VERSION = "nfc-lower-space-v1"
ID_VERSION = "create-event-uuid5-v1"
UNICODE_VERSION = unicodedata.unidata_version
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "tennis-predictor/player-identity/id-v1")
MAX_EVENTS = 10000
MAX_BYTES = 16 * 1024 * 1024


class IdentityError(ValueError):
    """Fixed codes only, never include private input in messages."""


def check(ok, code="invalid_event"):
    if not ok:
        raise IdentityError(code)


def text(value, limit=512):
    check(type(value) is str and 0 < len(value) <= limit, "invalid_text")
    check(not any(unicodedata.category(c).startswith("C") for c in value), "invalid_text")
    return value


def normalize_name(name):
    name = text(name, 200)
    check("/" not in name, "singles_only")
    # Deliberately preserve accents, punctuation, initials, order and scripts.
    # lower(), not casefold(): do not expand e.g. German sharp-s into ss.
    result = unicodedata.normalize("NFC", " ".join(unicodedata.normalize("NFC", name).lower().split()))
    check(bool(result) and any(c.isalpha() for c in result), "invalid_name")
    return result


def event_uuid(value):
    try:
        check(type(value) is str)
        check(str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError, TypeError):
        raise IdentityError("invalid_uuid") from None
    return value


def instant(value):
    try:
        check(type(value) is str)
        result = datetime.fromisoformat(value)
        check(result.tzinfo is not None and result.utcoffset() is not None)
        return result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise IdentityError("invalid_time") from None


def canonical(data):
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def unique_pairs(pairs):
    result = {}
    for k, v in pairs:
        check(k not in result, "duplicate_json_key")
        result[k] = v
    return result


@dataclass(frozen=True)
class Resolution:
    status: str
    reason: str
    player_id: str | None
    candidates: tuple[str, ...]
    registry_digest: str
    revision: int
    normalization_version: str = NORMALIZATION_VERSION
    verified: bool = field(default=False, init=False)
    unicode_version: str = UNICODE_VERSION


class PlayerRegistry:
    """Single-writer journal. apply() is explicit, resolve() never mutates state."""
    def __init__(self, *, projection=False):
        # A per-match projection is ephemeral and may have no recorded decision time.
        self._projection = projection
        self._events = []
        self._event_ids = {}
        self._players = {}
        self._aliases = {}
        self._retracted = set()

    def __repr__(self):
        return "<PlayerRegistry revision=" + str(self.revision) + ">"

    @property
    def revision(self):
        return len(self._events)

    @property
    def events(self):
        return copy.deepcopy(self._events)

    @property
    def digest(self):
        return hashlib.sha256(canonical(self._envelope()).encode()).hexdigest()

    def _envelope(self):
        return {"schema_version": SCHEMA_VERSION, "normalization_version": NORMALIZATION_VERSION,
                "id_version": ID_VERSION, "unicode_version": UNICODE_VERSION, "events": self.events}

    def apply(self, event):
        check(type(event) is dict)
        # JSON round-trip isolates callers' mutable structures; reject non-JSON data.
        try:
            event = json.loads(canonical(event), object_pairs_hook=unique_pairs)
        except (ValueError, TypeError, RecursionError):
            raise IdentityError("invalid_event") from None
        op = event.get("op")
        expected = {
            "create": {"event_id", "op", "gender", "name", "provenance"},
            "alias": {"event_id", "op", "player_id", "name", "provenance"},
            "retract": {"event_id", "op", "alias_event_id", "provenance"},
        }
        check(type(op) is str and op in expected and set(event) == expected[op], "invalid_event_schema")
        eid = event_uuid(event["event_id"])
        if eid in self._event_ids:
            check(self._event_ids[eid] == event, "event_conflict")
            return self._result_id(event)
        check(self.revision < MAX_EVENTS, "event_limit")
        p = event["provenance"]
        check(type(p) is dict and set(p) == {
            "source", "reference", "actor", "observed_at", "recorded_at"}, "invalid_provenance")
        for key in ("source", "reference", "actor"):
            text(p[key])
        recorded = p["recorded_at"]
        if recorded is not None or not self._projection:
            recorded = instant(recorded)
        if p["observed_at"] is not None:
            check(recorded is not None and instant(p["observed_at"]) <= recorded, "future_evidence")
        if self._events:
            previous = self._events[-1]["provenance"]["recorded_at"]
            check((recorded is None and previous is None) or (
                recorded is not None and previous is not None and recorded >= instant(previous)
            ), "journal_order")
        if op in ("create", "alias"):
            normalized = normalize_name(event["name"])
            if op == "create":
                check(event["gender"] in ("M", "F"), "invalid_gender")
                pid = str(uuid.uuid5(NAMESPACE, eid))
                check(pid not in self._players, "identity_conflict")
                gender = event["gender"]
            else:
                pid = event_uuid(event["player_id"])
                check(pid in self._players, "unknown_player")
                gender = self._players[pid]
        else:
            aid = event_uuid(event["alias_event_id"])
            check(aid in self._aliases and aid not in self._retracted, "unknown_alias")
        proposed = self._envelope()
        proposed["events"].append(event)
        check(len(canonical(proposed).encode()) <= MAX_BYTES - 128, "snapshot_limit")
        # All validation precedes mutation. No merge/reassignment operation exists.
        if op == "create":
            self._players[pid] = gender
        if op in ("create", "alias"):
            self._aliases[eid] = (pid, gender, normalized)
        else:
            self._retracted.add(aid)
        self._events.append(event)
        self._event_ids[eid] = event
        return self._result_id(event)

    def _result_id(self, event):
        if event["op"] == "create":
            return str(uuid.uuid5(NAMESPACE, event["event_id"]))
        if event["op"] == "alias":
            return event["player_id"]
        return None

    def resolve(self, gender, name):
        if gender not in ("M", "F"):
            return Resolution("quarantine", "unknown_gender", None, (), self.digest, self.revision)
        try:
            normalized = normalize_name(name)
        except IdentityError:
            return Resolution("quarantine", "invalid_name", None, (), self.digest, self.revision)
        candidates = tuple(sorted({pid for eid, (pid, g, n) in self._aliases.items()
                                   if eid not in self._retracted and g == gender and n == normalized}))
        if not candidates:
            return Resolution("quarantine", "unmatched_name", None, (), self.digest, self.revision)
        if len(candidates) > 1:
            return Resolution("quarantine", "ambiguous_name", None, candidates, self.digest, self.revision)
        return Resolution("provisional", "name_only", candidates[0], candidates, self.digest, self.revision)

    def at_revision(self, revision):
        check(type(revision) is int and 0 <= revision <= self.revision, "invalid_revision")
        return self.replay(self._events[:revision], projection=self._projection)

    @classmethod
    def replay(cls, events, *, projection=False):
        check(type(events) in (list, tuple) and len(events) <= MAX_EVENTS, "event_limit")
        registry = cls(projection=projection)
        seen = set()
        for event in events:
            check(type(event) is dict and type(event.get("event_id")) is str, "invalid_event")
            check(event["event_id"] not in seen, "duplicate_event")
            seen.add(event["event_id"])
            registry.apply(event)
        return registry

    def to_json(self):
        check(not self._projection, "ephemeral_projection")
        envelope = self._envelope()
        envelope["digest"] = self.digest
        raw = canonical(envelope)
        check(len(raw.encode()) <= MAX_BYTES, "snapshot_limit")
        return raw

    @classmethod
    def from_json(cls, raw):
        check(type(raw) is str and len(raw.encode()) <= MAX_BYTES, "snapshot_limit")
        try:
            d = json.loads(raw, object_pairs_hook=unique_pairs)
        except (ValueError, TypeError, RecursionError):
            raise IdentityError("invalid_snapshot") from None
        check(type(d) is dict and set(d) == {
            "schema_version", "normalization_version", "id_version", "unicode_version", "events", "digest"}, "invalid_snapshot")
        check(d["schema_version"] == SCHEMA_VERSION and d["normalization_version"] == NORMALIZATION_VERSION
              and d["id_version"] == ID_VERSION and d["unicode_version"] == UNICODE_VERSION, "unsupported_version")
        registry = cls.replay(d["events"])
        check(type(d["digest"]) is str and d["digest"] == registry.digest, "digest_mismatch")
        return registry

    def save(self, path):
        """Explicit local atomic no-overwrite publication; private file mode 0600."""
        raw = self.to_json()
        target = Path(path)
        fd, temporary = tempfile.mkstemp(prefix=".identity-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-link within the same directory: atomic and no replacement.
            os.link(temporary, target)
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            os.unlink(temporary)

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        check(len(raw) <= MAX_BYTES, "snapshot_limit")
        try:
            return cls.from_json(raw.decode("utf-8"))
        except UnicodeError:
            raise IdentityError("invalid_snapshot") from None
