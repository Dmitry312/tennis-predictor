"""Versioned last-match windows over one bounded history read per player."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from statistics import fmean, pstdev

VERSION = "player-form-v1"
WINDOWS = (5, 10, 15)


@dataclass(frozen=True)
class Observation:
    match: str
    day: date
    revision: str
    values: dict


def metric_pairs(own, other):
    """Inputs are scope-zero, compatible valid counters for the two source sides."""
    output = {}

    def read(values, *keys):
        items = [values.get(key) for key in keys]
        return None if None in items else sum(items)

    def ratio(name, count, denom):
        if count is not None and denom and 0 <= count <= denom:
            output[name] = (count, denom)

    for name, metric in (("win", "match_win"), ("sets", "sets_won"), ("games", "games_won")):
        ratio(
            name,
            own.get(metric),
            1
            if name == "win"
            else read(own, metric, "sets_lost" if name == "sets" else "games_lost"),
        )
    sw, rw, so, ro = [], [], [], []
    for prefix in ("first", "second"):
        won, all_ = f"{prefix}_serve_points_won", f"{prefix}_serve_points_all"
        a, b, c, d = own.get(won), own.get(all_), other.get(won), other.get(all_)
        return_won = d - c if c is not None and d is not None else None
        ratio(f"{prefix}_serve_won", a, b)
        ratio(f"{prefix}_return_won", return_won, d)
        sw.append(a)
        so.append(b)
        rw.append(return_won)
        ro.append(d)
    serve_total = None if None in so else sum(so)
    return_total = None if None in ro else sum(ro)
    ratio("first_serve_in", so[0], serve_total)
    ratio("serve_points", None if None in sw else sum(sw), serve_total)
    ratio("return_points", None if None in rw else sum(rw), return_total)
    ratio(
        "points",
        None if None in sw + rw else sum(sw + rw),
        None if None in (serve_total, return_total) else serve_total + return_total,
    )
    for name in ("aces", "double_faults"):
        ratio(name, own.get(name), serve_total)
    ratio("hold", own.get("service_games_won"), own.get("service_games_all"))
    for name, source, won, total in (
        ("break", other, "service_games_won", "service_games_all"),
        ("break_points_saved", other, "break_points_won", "break_points_all"),
        ("break_points_converted", own, "break_points_won", "break_points_all"),
    ):
        a, b = source.get(won), source.get(total)
        ratio(name, b - a if name != "break_points_converted" and None not in (a, b) else a, b)
    for name in ("winners", "unforced_errors"):
        if own.get(name) is not None:
            output[name] = (own[name], 1)
    return output


METRICS = (
    "win",
    "sets",
    "games",
    "points",
    "first_serve_in",
    "first_serve_won",
    "second_serve_won",
    "serve_points",
    "return_points",
    "first_return_won",
    "second_return_won",
    "aces",
    "double_faults",
    "hold",
    "break",
    "break_points_saved",
    "break_points_converted",
    "winners",
    "unforced_errors",
)


def observations(rows, player, day, revision, mid):
    """Convert a Match Data Store match into the shared feature input."""
    groups = defaultdict(list)
    for row in rows:
        if row["scope"] == 0 and row["history_eligible"] and row["quality"] == "VALID":
            groups[row["side"], row["metric"]].append(row)
    side = next((row["side"] for row in rows if row["player_id"] == player), None)
    if side is None or any(len(groups[i, "match_win"]) != 1 for i in (1, 2)):
        return None
    sides = [groups[i, "match_win"][0] for i in (1, 2)]
    if {row["numerator"] for row in sides} != {0, 1}:
        return None
    if any(not row["player_id"] for row in sides) or sides[0]["player_id"] == sides[1]["player_id"]:
        return None
    counters = [
        {
            metric: items[0]["numerator"]
            for (s, metric), items in groups.items()
            if s == side_no and len(items) == 1
        }
        for side_no in (side, 3 - side)
    ]
    return Observation(mid, day, revision, metric_pairs(*counters))


def windows(matches, prediction_date):
    selected = sorted(
        {m.match: m for m in matches if m.day < prediction_date}.values(),
        key=lambda m: (m.day, m.match),
        reverse=True,
    )
    out = {}
    for size in WINDOWS:
        sample = selected[:size]
        metrics = {}
        for name in METRICS:
            values = [m.values[name] for m in sample if name in m.values]
            ratios = [a / b for a, b in values]
            denominator = sum(b for _, b in values)
            metrics[name] = {
                "pooled": sum(a for a, _ in values) / denominator if values else None,
                "mean": fmean(ratios) if ratios else None,
                "stddev": pstdev(ratios) if len(ratios) > 1 else None,
                "valid": len(values),
                "denominator": denominator if values else None,
                "missing": len(sample) - len(values),
            }
        out[size] = {
            "matches": len(sample),
            "span_days": (sample[0].day - sample[-1].day).days if sample else None,
            "metrics": metrics,
        }
    return out


class Features:
    """Cache vectors by immutable source version, player and cutoff."""

    def __init__(self, recent, max_cache=8192):
        self.recent = recent
        self.get = lru_cache(maxsize=max_cache)(self._read)

    def _read(self, source_version, player, day, cutoff=None):
        return windows(self.recent(player, day, cutoff), day)
