"""Transform one match into observations without computing historical features."""

import json
import re
from collections import Counter, defaultdict

from tennis_predictor.ml.canonical_layer.contracts import METRICS
from tennis_predictor.ml.canonical_layer.evidence import encoded

from .contracts import REF

RATIOS = {
    "first_serve_win_rate": ("first_serve_points_won", "first_serve_points_all"),
    "second_serve_win_rate": ("second_serve_points_won", "second_serve_points_all"),
    "service_game_win_rate": ("service_games_won", "service_games_all"),
    "break_conversion_rate": ("break_points_won", "break_points_all"),
}


def provenance(*rows):
    return [{name: row.get(name) for name in REF.names} for row in rows]


def ratio_quality(statuses, denominator):
    if "CONFLICT" in statuses:
        return "CONFLICT"
    if "UNVERIFIED" in statuses:
        return "UNVERIFIED"
    if all(s == "MISSING" for s in statuses):
        return "MISSING"
    return "VALID" if all(s == "VALID" for s in statuses) and denominator else "PARTIAL"


class MatchAggregation:
    def __init__(self, match):
        self.tables = match.tables
        rows = self.tables["matches"]
        if len(rows) != 1 or rows[0]["match_id"] != match.match_id:
            raise ValueError("Expected one canonical match")
        if any(
            row.get("match_id", match.match_id) != match.match_id
            for table in self.tables.values()
            for row in table
        ):
            raise ValueError("Mixed canonical matches")
        self.match = rows[0]
        self.quality = defaultdict(list)
        for row in self.tables["data_quality"]:
            key = row["source_version"], row["category"], row["entity_ref"]
            self.quality[key].append(row)
        self.metrics = []
        player_rows = {r["player_id"]: r for r in self.tables["players"]}
        self.sides = {}
        for side in (1, 2):
            ref = self.match[f"player{side}_id"]
            verified = ref and player_rows.get(ref, {}).get("resolution_status") == "verified"
            self.sides[side] = {
                "match_id": match.match_id,
                "side": side,
                "match_date": self.match["match_date"],
                "player_id": ref if verified else None,
                "provisional_ref": ref if not verified else None,
                "identity_recorded_at": player_rows[ref].get("recorded_at") if verified else None,
            }

    def emit(
        self,
        family,
        metric,
        side,
        scope,
        row,
        quality,
        reason,
        *,
        value=None,
        denominator=None,
        samples=None,
        raw=None,
        game=None,
        complete=None,
        refs=None,
        unique=True,
    ):
        refs = refs or provenance(row)
        times = [r["available_at"] for r in refs]
        valid_times = all(
            item["available_at"] is not None
            and (item["ingested_at"] is None or item["available_at"] <= item["ingested_at"])
            and (item["recorded_at"] is None or item["available_at"] <= item["recorded_at"])
            and (
                self.match["start_time"] is None or item["available_at"] > self.match["start_time"]
            )
            for item in refs
        )
        eligible = (
            quality == "VALID"
            and unique
            and scope is not None
            and self.match["status"] == 3
            and self.match.get("duplicate_of") is None
        )
        self.metrics.append(
            {
                **self.sides[side],
                "family": family,
                "metric": metric,
                "scope": scope,
                "game": game,
                "source_version": row["source_version"],
                "numerator": value,
                "denominator": denominator,
                "sample_count": samples,
                "coverage": quality.lower(),
                "quality": quality,
                "reason": reason,
                "raw_value": raw,
                "available_at": max(times) if valid_times else None,
                "source_refs": refs,
                "game_complete": complete,
                "history_eligible": eligible,
            }
        )

    def results(self):
        results = self.tables["match_results"]
        for result in results:
            sets = result.get("sets") or []
            winner = result.get("winner_side")
            valid = result["status"] == 3 and winner in (1, 2)
            key = result["source_version"], "result", result["evidence_ref"]
            conflict = any(r["status"] == "conflict" for r in self.quality[key])
            quality = (
                "VALID"
                if valid
                else "CONFLICT"
                if conflict
                else "UNVERIFIED"
                if sets
                else "MISSING"
            )
            for side in (1, 2):
                self.emit(
                    "result",
                    "match_win",
                    side,
                    0,
                    result,
                    quality,
                    "canonical_result",
                    value=int(winner == side) if valid else None,
                    raw=result.get("sets_raw"),
                    samples=1 if valid else None,
                    unique=len(results) == 1,
                )
                self.score_facts(result, side, sets, valid, len(results) == 1)

    def score_facts(self, row, side, sets, valid, unique):
        games = []
        durations = []
        for scope, score in enumerate(sets, 1):
            own, other = score[f"games_p{side}"], score[f"games_p{3 - side}"]
            additive = valid and not (
                scope == row["best_of"] and row["decider_rule"] == "match_tiebreak10"
            )
            games.append((own, other) if additive else None)
            raw = score["duration_raw"]
            minutes = None
            if isinstance(raw, str) and re.fullmatch(r"[0-9]{2}:[0-5][0-9]", raw):
                hour, minute = map(int, raw.split(":"))
                minutes = hour * 60 + minute
            durations.append(minutes)
            quality = (
                "MISSING"
                if not raw
                else "CONFLICT"
                if minutes is None
                else "VALID"
                if valid
                else "PARTIAL"
            )
            self.emit(
                "result",
                "duration_minutes",
                side,
                scope,
                row,
                quality,
                "nbbet_duration_hhmm",
                value=minutes,
                raw=raw,
                unique=unique,
            )
            if valid:
                self.emit(
                    "result",
                    "set_win",
                    side,
                    scope,
                    row,
                    "VALID",
                    "completed_set",
                    value=int(own > other),
                    samples=1,
                    unique=unique,
                )
            if additive:
                self.game_totals(row, side, scope, own, other, unique)
            elif valid:
                for metric in ("games_won", "games_lost", "games_margin"):
                    self.emit(
                        "result",
                        metric,
                        side,
                        scope,
                        row,
                        "UNVERIFIED",
                        "match_tiebreak_points_are_not_games",
                    )
        complete = valid and durations and all(value is not None for value in durations)
        self.emit(
            "result",
            "duration_minutes",
            side,
            0,
            row,
            "VALID"
            if complete
            else "PARTIAL"
            if any(v is not None for v in durations)
            else "MISSING",
            "complete_set_durations_required",
            value=sum(durations) if complete else None,
            samples=sum(v is not None for v in durations),
            unique=unique,
        )
        if valid:
            won = sum(s[f"games_p{side}"] > s[f"games_p{3 - side}"] for s in sets)
            for metric, value in (
                ("sets_won", won),
                ("sets_lost", len(sets) - won),
                ("sets_margin", 2 * won - len(sets)),
            ):
                self.emit(
                    "result",
                    metric,
                    side,
                    0,
                    row,
                    "VALID",
                    "complete_sets",
                    value=value,
                    samples=len(sets),
                    unique=unique,
                )
            if games and all(g is not None for g in games):
                own, other = map(sum, zip(*games, strict=True))
                self.game_totals(row, side, 0, own, other, unique)
            else:
                for metric in ("games_won", "games_lost", "games_margin"):
                    self.emit(
                        "result",
                        metric,
                        side,
                        0,
                        row,
                        "UNVERIFIED",
                        "mixed_game_and_match_tiebreak_units",
                    )

    def game_totals(self, row, side, scope, own, other, unique):
        for metric, value in (
            ("games_won", own),
            ("games_lost", other),
            ("games_margin", own - other),
        ):
            self.emit(
                "result",
                metric,
                side,
                scope,
                row,
                "VALID",
                "legal_game_counts",
                value=value,
                samples=own + other,
                unique=unique,
            )

    def counter_quality(self, row, metric, duplicate):
        if duplicate or row["scope"] is None:
            return "CONFLICT", "duplicate_or_invalid_scope"
        ref = f"{row['evidence_ref']}/p{row['player_side']}/{metric}"
        checks = self.quality[row["source_version"], metric, ref]
        if len(checks) == 1:
            return checks[0]["status"].upper(), checks[0]["reason"]
        return (
            ("UNVERIFIED", "missing_counter_validation")
            if row[metric] is not None
            else ("MISSING", "counter_missing")
        )

    def stats(self):
        rows = self.tables["match_player_stats"]
        key = lambda r: (r["source_version"], r["manifest_id"], r["scope"], r["player_side"])
        by_key = defaultdict(list)
        for row in rows:
            by_key[key(row)].append(row)
        versions = {r["source_version"] for r in rows}
        for row in rows or [
            {**self.match, "scope": 0, "player_side": side, **dict.fromkeys(METRICS)}
            for side in (1, 2)
        ]:
            side, scope = row["player_side"], row["scope"]
            group = key(row)[:-1]
            oriented = self.match.get("source_match_id") is not None
            unique = len(versions) <= 1 and oriented
            refs = provenance(row)
            raw = json.loads(row.get("raw_json") or "{}")
            statuses = {}
            for metric in METRICS:
                quality, reason = self.counter_quality(row, metric, len(by_key[key(row)]) > 1)
                statuses[metric] = quality
                self.emit(
                    "stats",
                    metric,
                    side,
                    scope,
                    row,
                    quality,
                    reason,
                    value=row[metric],
                    samples=1 if row[metric] is not None else None,
                    raw=encoded(raw[metric]).decode().strip()
                    if raw.get(metric) is not None
                    else None,
                    refs=refs,
                    unique=unique,
                )
            for metric, (won, total) in RATIOS.items():
                self.emit(
                    "stats",
                    metric,
                    side,
                    scope,
                    row,
                    ratio_quality([statuses[won], statuses[total]], row[total]),
                    "paired_source_counts",
                    value=row[won],
                    denominator=row[total],
                    samples=row[total],
                    refs=refs,
                    unique=unique,
                )
            self.opponent_metrics(row, by_key[(*group, 3 - side)], unique, oriented)

    def opponent_metrics(self, row, opponents, unique, oriented):
        side, scope = row["player_side"], row["scope"]
        opponent = opponents[0] if len(opponents) == 1 else None
        for family, metric, won, total in (
            ("return", "first_return_win_rate", "first_serve_points_won", "first_serve_points_all"),
            (
                "return",
                "second_return_win_rate",
                "second_serve_points_won",
                "second_serve_points_all",
            ),
            ("stats", "break_points_saved", "break_points_won", "break_points_all"),
        ):
            value = denominator = None
            refs = provenance(row)
            if opponent is None:
                quality, reason = (
                    ("CONFLICT", "duplicate_opponent_scope")
                    if opponents
                    else ("MISSING", "opponent_scope_missing")
                )
            else:
                pair = [self.counter_quality(opponent, m, False)[0] for m in (won, total)]
                refs = provenance(row, opponent)
                if "CONFLICT" in pair:
                    quality, reason = "CONFLICT", "invalid_opponent_service_counts"
                elif pair == ["MISSING", "MISSING"]:
                    quality, reason = "MISSING", "opponent_service_counts_missing"
                elif not oriented:
                    quality, reason = "UNVERIFIED", "compatible_service_side_unproven"
                else:
                    quality, reason = (
                        ratio_quality(pair, opponent[total]),
                        "opponent_count_complement",
                    )
                    denominator = opponent[total]
                    if (
                        opponent[won] is not None
                        and denominator is not None
                        and opponent[won] <= denominator
                    ):
                        value = denominator - opponent[won]
            self.emit(
                family,
                metric,
                side,
                scope,
                row,
                quality,
                reason,
                value=value,
                denominator=denominator,
                samples=denominator,
                refs=refs,
                unique=unique,
            )
            if metric == "break_points_saved":
                self.emit(
                    family,
                    "break_points_faced",
                    side,
                    scope,
                    row,
                    quality,
                    reason,
                    value=denominator,
                    samples=denominator,
                    refs=refs,
                    unique=unique,
                )

    def pbp(self):
        games = defaultdict(list)
        for row in self.tables["match_player_pbp"]:
            games[
                row["source_version"], row["manifest_id"], row["set_num"], row["game_num"]
            ].append(row)
        if not games:
            for side in (1, 2):
                self.emit("pbp", "observed_points_won", side, 0, self.match, "MISSING", "no_pbp")
        versions = {g[0] for g in games}
        reconciliation_conflicts = {
            row["source_version"]
            for row in self.tables["data_quality"]
            if row["category"] in ("pbp_result_reconciliation", "pbp_statistics_reconciliation")
            and row["status"] == "conflict"
        }
        for (version, _, scope, game), points in sorted(
            games.items(), key=lambda item: str(item[0])
        ):
            first = points[0]
            prefix = first["evidence_ref"].rsplit("/rows/", 1)[0]
            direct = self.quality[version, "pbp", f"{prefix}/set/{scope}/game/{game}"]
            positions = Counter(p["point_num"] for p in points)
            deduped = {p["point_num"]: p for p in points if positions[p["point_num"]] == 1}
            conflict = (
                version in reconciliation_conflicts
                or scope is None
                or game is None
                or any(r["status"] == "conflict" for r in direct)
                or any(n > 1 for n in positions.values())
            )
            contiguous = set(positions) == set(range(1, len(points) + 1))
            complete_values = [p["game_complete_verified"] for p in points]
            complete = (
                True
                if all(v is True for v in complete_values)
                else False
                if False in complete_values
                else None
            )
            counts = Counter(
                p["winner_side"] for p in deduped.values() if p["winner_side"] in (1, 2)
            )
            quality = (
                "CONFLICT"
                if conflict
                else "VALID"
                if complete and contiguous and sum(counts.values()) == len(points)
                else "PARTIAL"
            )
            for side in (1, 2):
                self.emit(
                    "pbp",
                    "observed_points_won",
                    side,
                    scope,
                    first,
                    quality,
                    "observed_unambiguous_points_only",
                    value=counts[side] if counts else None,
                    denominator=sum(counts.values()) or None,
                    samples=sum(counts.values()) or None,
                    game=game,
                    complete=complete,
                    refs=provenance(*points),
                    unique=len(versions) == 1,
                )

    def run(self):
        self.results()
        self.stats()
        self.pbp()
        sides = [
            {
                **self.sides[side],
                "display_name": self.match[f"player{side}_raw"],
                "status": self.match["status"],
                "source_match_id": self.match["source_match_id"],
                "duplicate_of": self.match.get("duplicate_of"),
                "source_refs": provenance(self.match),
            }
            for side in (1, 2)
        ]
        return {"match_sides": sides, "metrics": self.metrics}
