"""Pure, fail-closed normalization for *review* examples, never model labels.

No IO and no logging of input rows. Output is a strict allowlist; identifiers,
player data, odds, and arbitrary strings cannot cross the boundary.
"""
import json
import math
import re

SURFACES = frozenset({"hard", "clay", "grass", "carpet"})
PBP_INT = frozenset({"set_num", "game_num", "point_num", "server", "point_winner"})
PBP_BOOL = frozenset({"server_won_point", "is_break_point", "is_match_point",
                      "is_tiebreak", "game_complete"})
STATS_INT = frozenset({"scope"})
STATS_NUMERIC = frozenset(
    f"{field}_p{player}" for field in (
        "aces", "double_faults", "first_serve_points_all",
        "first_serve_points_won", "second_serve_points_all",
        "second_serve_points_won", "break_points_won", "break_points_all",
        "winners", "unforced_errors", "max_points_in_row",
        "max_games_in_row", "service_games_won", "service_games_all",
    ) for player in (1, 2)
)
DETAIL_INT = frozenset({"status", "details_version"})
DETAIL_BOOL = frozenset({"has_coverage"})
SCORE = re.compile(r"^(?:0|15|30|40|AD|A|[0-9]{1,2})$")
POINT_TYPE = frozenset({"ace", "double_fault", "winner", "unforced_error"})


def number(value, *, integer=False):
    """Preserve null versus zero, reject bool, nonfinite and lossy casts."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if integer:
        return int(value) if int(value) == value else None
    return value


def boolean(value):
    return value if type(value) is bool else None


def safe_sets(raw):
    """Return only bounded numeric set scores, not arbitrary JSON or names."""
    if not isinstance(raw, str) or len(raw) > 4096:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list) or len(value) > 5:
        return None
    # Shape not yet verified: do not guess winner or positional semantics.
    # Emit only type/length evidence until the user reviews raw structures
    # in a protected environment; never export arbitrary nested payload.
    return {
        "kind": "list",
        "length": len(value),
        "element_types": [
            "list" if isinstance(x, list) else
            "dict" if isinstance(x, dict) else
            "null" if x is None else
            "other"
            for x in value
        ],
    }


def sanitize_row(prefix, row):
    """Strictly allowlisted primitive evidence; never include raw match IDs."""
    if not isinstance(row, dict):
        raise ValueError("invalid row")
    out = {}
    if prefix == "pbp/":
        for name in PBP_INT:
            if name in row:
                out[name] = number(row[name], integer=True)
        for name in PBP_BOOL:
            if name in row:
                out[name] = boolean(row[name])
        for name in ("score_server", "score_returner"):
            if name in row:
                v = row[name]
                out[name] = v if isinstance(v, str) and SCORE.fullmatch(v) else None
        if "point_type" in row:
            v = row["point_type"]
            out["point_type"] = v if isinstance(v, str) and v in POINT_TYPE else None
    elif prefix == "nbbet_stats/":
        for name in STATS_INT:
            if name in row:
                out[name] = number(row[name], integer=True)
        for name in STATS_NUMERIC:
            if name in row:
                out[name] = number(row[name])
    elif prefix == "nbbet_details/":
        for name in DETAIL_INT:
            if name in row:
                out[name] = number(row[name], integer=True)
        for name in DETAIL_BOOL:
            if name in row:
                out[name] = boolean(row[name])
        if "sets_json" in row:
            out["sets_structure"] = safe_sets(row["sets_json"])
        for name in ("p1_rank", "p2_rank"):
            if name in row:
                out[name] = number(row[name], integer=True)
    else:
        raise ValueError("unsupported prefix")
    return out


def surface_from_pg(value):
    """Only the PostgreSQL match surface is authoritative."""
    return value if type(value) is str and value in SURFACES else None
