"""Versioned Match Data Store contracts."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import pyarrow as pa

VERSION = "match-data-v1"
Quality = Literal["VALID", "MISSING", "PARTIAL", "CONFLICT", "UNVERIFIED"]
S, I, B = pa.string(), pa.int64(), pa.bool_()
T = pa.timestamp("us", tz="UTC")
D = pa.date32()
REF = pa.struct(
    [
        ("manifest_id", S),
        ("source_version", S),
        ("evidence_ref", S),
        ("content_sha256", S),
        ("available_at", T),
        ("ingested_at", T),
        ("recorded_at", T),
    ]
)
KEY = [
    ("match_id", S),
    ("side", I),
    ("match_date", D),
    ("player_id", S),
    ("provisional_ref", S),
    ("identity_recorded_at", T),
    ("registry_version", S),
    ("identity_provenance", S),
    ("match_quality_flags", pa.list_(pa.field("element", S))),
]
SCHEMAS = {
    "match_sides": pa.schema(
        KEY
        + [
            ("display_name", S),
            ("status", I),
            ("source_match_id", I),
            ("duplicate_of", S),
            ("source_refs", pa.list_(pa.field("element", REF))),
        ],
        metadata={b"contract": VERSION.encode()},
    ),
    "metrics": pa.schema(
        KEY
        + [
            ("family", S),
            ("metric", S),
            ("scope", I),
            ("game", I),
            ("source_version", S),
            ("numerator", I),
            ("denominator", I),
            ("sample_count", I),
            ("coverage", S),
            ("quality", S),
            ("reason", S),
            ("raw_value", S),
            ("available_at", T),
            ("source_refs", pa.list_(pa.field("element", REF))),
            ("game_complete", B),
            ("history_eligible", B),
        ],
        metadata={b"contract": VERSION.encode()},
    ),
}


@dataclass(frozen=True)
class MatchInput:
    """One pinned canonical match shard. No odds or ranking inputs are required."""

    match_id: str
    input_revision: str
    tables: dict[str, list[dict]]


def historical_before(match_date: date | None, prediction_date: date) -> bool:
    return match_date is not None and match_date < prediction_date


def utc_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(UTC)


def rate(observations):
    """Pool one metric, one side per match, at a single non-game scope."""
    rows = tuple(observations)
    if len({(r["metric"], r["scope"]) for r in rows}) != 1:
        return None
    if len({r["match_id"] for r in rows}) != len(rows) or any(r["game"] is not None for r in rows):
        return None
    if any(
        r["quality"] != "VALID"
        or r["numerator"] is None
        or r["denominator"] is None
        or r["denominator"] <= 0
        for r in rows
    ):
        return None
    return sum(r["numerator"] for r in rows) / sum(r["denominator"] for r in rows)
