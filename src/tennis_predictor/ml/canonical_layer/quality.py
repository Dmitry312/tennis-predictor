"""Version-local quality checks. Observations remain queryable on failure."""

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from tennis_predictor.ingestion.nbbet.pbp_parsing import SCORE_RANK

from .contracts import METRICS
from .evidence import fingerprint

MISSING = (None, "None", "")
SERVE = (
    ("first_serve_points_won", "first_serve_points_all"),
    ("second_serve_points_won", "second_serve_points_all"),
    ("service_games_won", "service_games_all"),
)
BREAK = ("break_points_won", "break_points_all")
NONADDITIVE = {"max_points_in_row", "max_games_in_row"}


def counter(value, minimum=0):
    if isinstance(value, bool) or value in MISSING:
        return None
    try:
        number = Decimal(str(value))
        if number.is_finite() and minimum <= number <= 2**63 - 1 and number == int(number):
            return int(number)
    except InvalidOperation:
        pass
    return None


def flag(value):
    if type(value) is bool:
        return value
    return {"True": True, "False": False}.get(str(value))


def statistics(ctx, observation):
    rows = [
        (index, row)
        for index, row in enumerate(observation.rows)
        if ctx.link(observation, index, row)
    ]
    if not rows:
        ctx.quality(observation, "statistics", "missing", "no_statistics_rows")
        for category in ("serve", "return", "break_points"):
            ctx.quality(observation, category, "missing", "no_statistics_rows")
        return
    scopes = defaultdict(set)
    for _, row in rows:
        scopes[counter(row.get("scope"))].add(fingerprint(row))
    versions = {scope: len(values) for scope, values in scopes.items()}
    complete_set_count = ctx.compatible_set_count(observation)
    scope_values = {
        (side, scope): {metric: counter(row.get(f"{metric}_p{side}")) for metric in METRICS}
        for _, row in rows
        if (scope := counter(row.get("scope"))) is not None and versions[scope] == 1
        for side in (1, 2)
    }
    reconciliation_conflicts = set()
    for side in (1, 2):
        set_scopes = {scope for s, scope in scope_values if s == side and scope > 0}
        for metric in sorted(set(METRICS) - NONADDITIVE):
            match = scope_values.get((side, 0), {}).get(metric)
            sets = [scope_values[side, scope][metric] for scope in sorted(set_scopes)]
            if (
                complete_set_count is None
                or set_scopes != set(range(1, complete_set_count + 1))
                or match is None
                or any(x is None for x in sets)
            ):
                status, reason = "unverified", "complete_compatible_set_scopes_unproven"
            elif match == sum(sets):
                status, reason = "valid", "match_total_matches_sets"
            else:
                status, reason = "conflict", "match_total_differs_from_sets"
                reconciliation_conflicts.add((side, metric))
            ctx.quality(
                observation,
                f"{metric}_reconciliation",
                status,
                reason,
                f"{observation.reference}/p{side}/{metric}",
            )
    seen = set()
    for index, row in rows:
        row_hash = fingerprint(row)
        if row_hash in seen:
            ctx.quality(
                observation,
                "statistics",
                "partial",
                "duplicate_scope_observation",
                ctx.ref(observation, index),
            )
            continue
        seen.add(row_hash)
        scope = counter(row.get("scope"))
        ref = ctx.ref(observation, index)
        if scope is None or versions[scope] != 1:
            ctx.quality(observation, "statistics", "conflict", "invalid_or_divergent_scope", ref)
        eligible_at_cutoff = ctx.feature_time("statistics", observation, index)
        for side in (1, 2):
            raw = {metric: row.get(f"{metric}_p{side}") for metric in METRICS}
            values = {metric: counter(value) for metric, value in raw.items()}
            ctx.emit(
                "match_player_stats",
                observation,
                index,
                player_side=side,
                scope=scope,
                raw_json=ctx.json(raw),
                **values,
            )

            def quality(
                category,
                status,
                reason,
                *,
                entity=f"{ref}/p{side}",
                time_eligible=eligible_at_cutoff,
            ):
                ctx.quality(
                    observation,
                    category,
                    status,
                    reason,
                    f"{entity}/{category}",
                    eligible=status == "valid" and time_eligible,
                )

            invalid = {
                metric
                for metric in METRICS
                if values[metric] is None and raw[metric] not in MISSING
            }
            for won, total in (*SERVE, BREAK):
                if (
                    values[won] is not None
                    and values[total] is not None
                    and values[won] > values[total]
                ):
                    invalid.update((won, total))
            if scope == 0:
                invalid.update(metric for s, metric in reconciliation_conflicts if s == side)
            for metric in METRICS:
                if metric in invalid or scope is None or versions[scope] != 1:
                    quality(metric, "conflict", "invalid_counter_ratio_or_scope")
                elif values[metric] is None:
                    quality(metric, "missing", "counter_missing")
                else:
                    quality(metric, "valid", "counter_valid")
            for category, fields in (
                ("serve", {"aces", "double_faults", *(m for pair in SERVE for m in pair)}),
                ("break_points", set(BREAK)),
            ):
                if scope is None or versions[scope] != 1 or fields & invalid:
                    quality(category, "conflict", "invalid_group_counter_or_scope")
                elif all(values[field] is not None for field in fields):
                    quality(category, "valid", "complete_count_group")
                elif all(values[field] is None for field in fields):
                    quality(category, "missing", "group_missing")
                else:
                    quality(category, "partial", "group_incomplete")
            opposite = 3 - side
            pairs = [
                (counter(row.get(f"{won}_p{opposite}")), counter(row.get(f"{total}_p{opposite}")))
                for won, total in SERVE[:2]
            ]
            if (
                scope is None
                or versions[scope] != 1
                or any(
                    won is not None and total is not None and won > total for won, total in pairs
                )
                or (
                    scope == 0
                    and any(
                        (opposite, metric) in reconciliation_conflicts
                        for pair in SERVE[:2]
                        for metric in pair
                    )
                )
            ):
                quality("return", "conflict", "invalid_opponent_service_counts")
            elif all(won is not None and total is not None for won, total in pairs):
                quality("return", "valid", "return_counts_from_opponent_service")
            elif all(won is None and total is None for won, total in pairs):
                quality("return", "missing", "opponent_service_counts_missing")
            else:
                quality("return", "partial", "opponent_service_counts_incomplete")


def next_score(score, winner):
    a, b = score if winner == 1 else score[::-1]
    if (a == 3 and b < 3) or a == 4:
        return None  # A terminal point requires an explicit terminal score observation.
    if a == 3 and b == 4:
        expected = (3, 3)
    else:
        expected = (a + 1, b)
    return expected if winner == 1 else expected[::-1]


def point_by_point(ctx, observation):
    groups = defaultdict(list)
    seen = set()
    for index, row in enumerate(observation.rows):
        if not ctx.link(observation, index, row):
            continue
        # Keep every observation; sequence checks use one representative for exact duplicates.
        group = tuple(counter(row.get(key), 1) for key in ("set_num", "game_num"))
        groups[group].append((index, row))
    if not groups:
        ctx.quality(observation, "pbp", "missing", "no_point_rows")
        return
    by_set = defaultdict(set)
    server_by_game = {}
    for (set_num, game_num), points in groups.items():
        if set_num is not None and game_num is not None:
            by_set[set_num].add(game_num)
        positions = defaultdict(set)
        servers, tiebreaks = set(), set()
        for index, row in points:
            pos = counter(row.get("point_num"), 1)
            positions[pos].add(fingerprint(row))
            servers.add(counter(row.get("server"), 1))
            tiebreaks.add(flag(row.get("is_tiebreak")))
        if (
            set_num is not None
            and game_num is not None
            and tiebreaks == {False}
            and len(servers) == 1
        ):
            server_by_game[set_num, game_num] = next(iter(servers))
        reasons = set()
        if len(points) > sum(map(len, positions.values())):
            reasons.add("duplicate_point_observation")
        if (
            None in (set_num, game_num)
            or None in positions
            or any(len(hashes) > 1 for hashes in positions.values())
        ):
            reasons.add("invalid_or_divergent_point_key")
        valid_positions = sorted(p for p in positions if p is not None)
        if valid_positions != list(range(1, len(valid_positions) + 1)):
            reasons.add("point_sequence_gap")
        if len(servers) != 1 or len(tiebreaks) != 1 or None in tiebreaks:
            reasons.add("inconsistent_game_metadata")
        prev = (0, 0)
        for index, row in sorted(
            points, key=lambda item: counter(item[1].get("point_num"), 1) or 0
        ):
            server = counter(row.get("server"), 1)
            winner = counter(row.get("point_winner"), 1)
            tiebreak = flag(row.get("is_tiebreak"))
            point_num = counter(row.get("point_num"), 1)
            a, b = row.get("score_server"), row.get("score_returner")
            won = flag(row.get("server_won_point"))
            row_hash = fingerprint(row)
            if (row.get("server") not in MISSING and server not in (1, 2)) or (
                row.get("point_winner") not in MISSING and winner not in (1, 2)
            ):
                reasons.add("invalid_server_or_winner_side")
            if row_hash not in seen:
                seen.add(row_hash)
                if tiebreak is False and server in (1, 2):
                    current = (
                        SCORE_RANK.get(a) if isinstance(a, str) else None,
                        SCORE_RANK.get(b) if isinstance(b, str) else None,
                    )
                    if None in current or winner not in (1, 2):
                        reasons.add("score_or_winner_incomplete")
                    elif None not in prev:
                        relative_winner = 1 if server == winner else 2
                        if next_score(prev, relative_winner) != current or (
                            won is not None and won != (server == winner)
                        ):
                            reasons.add("score_transition_or_winner_conflict")
                    prev = current
                elif tiebreak is False:
                    reasons.add("score_or_winner_incomplete")
            ctx.emit(
                "match_player_pbp",
                observation,
                index,
                set_num=set_num,
                game_num=game_num,
                point_num=point_num,
                server_side=server,
                winner_side=winner,
                score_server=a,
                score_returner=b,
                server_won_point=won,
                is_tiebreak=tiebreak,
                is_break_point=flag(row.get("is_break_point")),
                is_match_point=flag(row.get("is_match_point")),
                game_complete_source=flag(row.get("game_complete")),
                game_complete_verified=False if tiebreak is False else None,
                point_type=row.get("point_type"),
                raw_json=ctx.json(row),
            )
        for reason in sorted(reasons or {"terminal_points_unproven"}):
            status = (
                "conflict"
                if reason
                in {
                    "invalid_or_divergent_point_key",
                    "inconsistent_game_metadata",
                    "invalid_server_or_winner_side",
                    "score_transition_or_winner_conflict",
                }
                else "partial"
            )
            ctx.quality(
                observation,
                "pbp",
                status,
                reason,
                f"{observation.reference}/set/{set_num}/game/{game_num}",
            )
    for set_num, games in sorted(by_set.items()):
        if sorted(games) != list(range(1, len(games) + 1)):
            ctx.quality(
                observation,
                "pbp",
                "partial",
                "game_sequence_gap",
                f"{observation.reference}/set/{set_num}",
            )
    for (set_num, game_num), server in sorted(server_by_game.items()):
        if server in (1, 2) and server_by_game.get((set_num, game_num - 1)) == server:
            ctx.quality(
                observation,
                "pbp",
                "conflict",
                "consecutive_games_same_server",
                f"{observation.reference}/set/{set_num}/game/{game_num}",
            )
    unique = {fingerprint(row): row for rows in groups.values() for _, row in rows}
    ctx.compare_pbp(
        observation, by_set, Counter(counter(row.get("server"), 1) for row in unique.values())
    )
