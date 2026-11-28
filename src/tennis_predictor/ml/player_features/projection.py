"""Compact historical adapter using the canonical statistics quality checks."""

import json
from types import SimpleNamespace

from tennis_predictor.ml.baselines.projection import latest_details
from tennis_predictor.ml.canonical_layer.contracts import METRICS
from tennis_predictor.ml.canonical_layer.evidence import fingerprint, instant
from tennis_predictor.ml.canonical_layer.quality import statistics

from .engine import Observation, metric_pairs


class ScopeQuality:
    def __init__(self, compatible_set_count):
        self.count = compatible_set_count
        self.values = {}
        self.valid = set()

    def link(self, obs, index, row):
        return True

    def compatible_set_count(self, obs):
        return self.count

    def quality(self, obs, category, status, reason, entity=None, **kw):
        if category in METRICS and status == "valid":
            self.valid.add(entity)

    def emit(self, table, obs, index, *, player_side, scope, raw_json, **values):
        if scope == 0:
            self.values[index, player_side] = values

    def feature_time(self, *args):
        return True

    def ref(self, obs, index):
        return str(index)

    def json(self, value):
        return ""


def latest_stats(rows, ids, as_of):
    latest = {}
    for row in rows:
        mid = row["canonical_match_id"]
        if mid not in ids:
            continue
        when = instant(row.get("fetched_at"), naive_utc=True)
        if when is None or when > as_of:
            continue
        if mid not in latest or when > latest[mid][0]:
            latest[mid] = (when, {})
        if when == latest[mid][0]:
            latest[mid][1][fingerprint(row)] = row
    return latest


def facts(matches, details, stats, as_of):
    results = latest_details(details, as_of)
    state = latest_stats(stats, {m.match_id for m in matches}, as_of)
    for match in matches:
        received, evidence = results[match.match_id]
        ((_, _, raw),) = evidence
        sets = json.loads(raw)
        score = [(s["score_team1"], s["score_team2"]) for s in sets]
        evidence_rows = state.get(match.match_id)
        aggregate = [{}, {}]
        if evidence_rows:
            stat_received, data = evidence_rows
            quality = ScopeQuality(len(sets) if stat_received == received else None)
            statistics(quality, SimpleNamespace(rows=list(data.values()), reference="s3"))
            for (idx, side), values in quality.values.items():
                aggregate[side - 1] = {
                    key: value
                    for key, value in values.items()
                    if f"{idx}/p{side}/{key}" in quality.valid
                }
        for side in (0, 1):
            own = aggregate[side]
            own["match_win"] = match.score if side == 0 else 1 - match.score
            own["sets_won"] = sum(s[side] > s[1 - side] for s in score)
            own["sets_lost"] = len(score) - own["sets_won"]
            own["games_won"] = sum(s[side] for s in score)
            own["games_lost"] = sum(s[1 - side] for s in score)
        revision = fingerprint(
            [
                match.revision,
                evidence_rows[0].isoformat() if evidence_rows else None,
                sorted(evidence_rows[1]) if evidence_rows else [],
            ]
        )
        yield (
            match,
            tuple(
                Observation(
                    match.match_id,
                    match.day,
                    revision,
                    metric_pairs(aggregate[i], aggregate[1 - i]),
                )
                for i in (0, 1)
            ),
        )
