"""Source content and time primitives shared with the offline catalog auditor."""

import json
from datetime import UTC, datetime
from hashlib import sha256


def encoded(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def digest(data):
    return sha256(data).hexdigest()


def fingerprint(value):
    return digest(encoded(value))


def instant(value, *, naive_utc=False):
    if not isinstance(value, str):
        return None
    try:
        if "T" not in value and " " not in value:
            return None
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not naive_utc:
            return None
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
