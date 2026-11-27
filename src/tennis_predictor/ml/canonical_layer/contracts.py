"""Canonical Layer v1 physical contracts and temporal policy."""

from datetime import UTC

import pyarrow as pa

VERSION = "canonical-v1"
MODES = ("strict_as_of", "historical_research_replay")
METRICS = (
    "aces",
    "double_faults",
    "first_serve_points_all",
    "first_serve_points_won",
    "second_serve_points_all",
    "second_serve_points_won",
    "break_points_won",
    "break_points_all",
    "winners",
    "unforced_errors",
    "max_points_in_row",
    "max_games_in_row",
    "service_games_won",
    "service_games_all",
)

S, I, B = pa.string(), pa.int64(), pa.bool_()
T, D = pa.timestamp("us", tz="UTC"), pa.date32()
SET = pa.struct(
    [
        ("games_p1", I),
        ("games_p2", I),
        ("duration_raw", S),
        ("tiebreak_p1", I),
        ("tiebreak_p2", I),
    ]
)
COMMON = [
    ("manifest_id", S),
    ("event_time", T),
    ("available_at", T),
    ("availability_evidence_ref", S),
    ("ingested_at", T),
    ("recorded_at", T),
    ("prediction_cutoff", T),
    ("source_version", S),
    ("content_sha256", S),
    ("evidence_status", S),
    ("evidence_ref", S),
    ("transform_version", S),
]
FIELDS = {
    "players": [
        ("player_id", S),
        ("gender", S),
        ("display_name", S),
        ("resolution_status", S),
        ("registry_version", S),
        ("identity_provenance", S),
    ],
    "player_aliases": [
        ("alias_id", S),
        ("player_id", S),
        ("match_id", S),
        ("player_side", I),
        ("source_name", S),
        ("normalized_name", S),
        ("source_player_id", S),
        ("registry_version", S),
        ("identity_provenance", S),
    ],
    "tournaments": [
        ("tournament_id", S),
        ("edition_id", S),
        ("name_raw", S),
        ("level", S),
        ("best_of", I),
        ("decider_rule", S),
    ],
    "matches": [
        ("match_id", S),
        ("source_match_id", I),
        ("player1_id", S),
        ("player2_id", S),
        ("player1_raw", S),
        ("player2_raw", S),
        ("edition_id", S),
        ("start_time", T),
        ("match_date", D),
        ("gender", S),
        ("level", S),
        ("surface", S),
        ("tournament_raw", S),
        ("duplicate_of", S),
        ("status", I),
    ],
    "match_results": [
        ("match_id", S),
        ("status", I),
        ("sets", pa.list_(pa.field("element", SET))),
        ("sets_raw", S),
        ("orientation", S),
        ("best_of", I),
        ("decider_rule", S),
        ("candidate_rule", S),
        ("source_candidate_winner_side", I),
        ("candidate_winner_side", I),
        ("winner_side", I),
        ("target_p1_win", I),
        ("target_eligible", B),
        ("exclusion_reason", S),
        ("player_state_eligible", B),
    ],
    "match_player_stats": [
        ("match_id", S),
        ("player_side", I),
        ("scope", I),
        *[(m, I) for m in METRICS],
        ("raw_json", S),
    ],
    "match_player_pbp": [
        ("match_id", S),
        ("set_num", I),
        ("game_num", I),
        ("point_num", I),
        ("server_side", I),
        ("winner_side", I),
        ("score_server", S),
        ("score_returner", S),
        ("server_won_point", B),
        ("is_break_point", B),
        ("is_match_point", B),
        ("is_tiebreak", B),
        ("game_complete_source", B),
        ("game_complete_verified", B),
        ("point_type", S),
        ("raw_json", S),
    ],
    "player_rankings": [
        ("match_id", S),
        ("player_side", I),
        ("player_id", S),
        ("tour", S),
        ("rank", I),
        ("effective_at", T),
    ],
    "match_odds": [
        ("match_id", S),
        ("price_p1", pa.decimal128(7, 3)),
        ("price_p2", pa.decimal128(7, 3)),
        ("captured_at", T),
        ("source_status", I),
        ("bookmaker", S),
        ("odds_source", S),
        ("model_feature_eligible", B),
    ],
    "data_quality": [
        ("quality_id", S),
        ("match_id", S),
        ("source", S),
        ("category", S),
        ("entity_ref", S),
        ("status", S),
        ("coverage_state", S),
        ("reason", S),
        ("rule_version", S),
        ("feature_eligible", B),
    ],
    "data_manifest": [
        ("match_id", S),
        ("source", S),
        ("source_ref", S),
        ("object_key", S),
        ("object_version", S),
        ("etag", S),
        ("object_last_modified", T),
        ("object_sha256", S),
        ("schema_sha256", S),
        ("selection_plan", S),
        ("operation_ref", S),
        ("coverage_state", S),
        ("input_rows", I),
    ],
}
REQUIRED = {
    "manifest_id",
    "source_version",
    "content_sha256",
    "evidence_status",
    "evidence_ref",
    "transform_version",
}
KEYS = {
    "players": {"player_id"},
    "player_aliases": {"alias_id", "match_id", "player_side", "player_id"},
    "tournaments": {"edition_id", "tournament_id"},
    "matches": {"match_id"},
    "match_results": {"match_id"},
    "match_player_stats": {"match_id", "player_side"},
    "match_player_pbp": {"match_id"},
    "player_rankings": {"match_id", "player_side"},
    "match_odds": {"match_id", "model_feature_eligible"},
    "data_quality": {
        "quality_id",
        "category",
        "match_id",
        "status",
        "reason",
        "rule_version",
        "source",
    },
    "data_manifest": {"match_id", "source", "source_ref"},
}
SCHEMAS = {
    name: pa.schema(
        [pa.field(k, t, nullable=k not in REQUIRED | KEYS[name]) for k, t in COMMON + fields],
        metadata={b"canonical_contract": VERSION.encode(), b"table": name.encode()},
    )
    for name, fields in FIELDS.items()
}


def temporal_gate(kind, *, mode, cutoff, available_at=None, event_date=None, effective_at=None):
    """Return eligibility and reason for a particular prediction cutoff."""
    if mode not in MODES:
        raise ValueError("unknown temporal mode")
    if kind in ("odds", "recommendations"):
        return False, "forbidden_model_input"
    if cutoff is None or cutoff.utcoffset() is None:
        return False, "cutoff_unknown"
    if kind == "ranking" and (effective_at is None or effective_at > cutoff):
        return False, "ranking_effective_time_unknown_or_future"
    if kind in ("result", "statistics", "pbp") and (
        event_date is None or event_date >= cutoff.astimezone(UTC).date()
    ):
        return False, "event_order_unproven"
    if available_at is not None and available_at < cutoff:
        return True, "proven_available"
    if mode == "historical_research_replay" and kind in ("result", "statistics", "pbp"):
        return True, "assumed_after_match_date"
    if available_at is not None:
        return False, "not_yet_available"
    return False, "availability_unknown"
