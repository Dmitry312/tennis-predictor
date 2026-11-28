"""Project PostgreSQL metadata and NBBet details into canonical result facts."""

import re
from collections import Counter, defaultdict
from datetime import date

from tennis_predictor.ml.canonical_layer.evidence import fingerprint, instant
from tennis_predictor.ml.canonical_layer.identity import PinnedIdentities
from tennis_predictor.ml.canonical_layer.transform import completed_set_winner
from tennis_predictor.ml.canonical_match import ContractError
from tennis_predictor.ml.ratings.inputs import SURFACES, Match

START = date(2021, 1, 1)
EXCLUDED = re.compile(
    r"\b(?:itf|davis\s+cup|billie\s+jean\s+king\s+cup|bjk\s+cup|fed\s+cup|"
    r"federation\s+cup|asian\s+games|exhibitions?|doubles)\b"
    r"|кубок\s+(?:дэвиса|девиса|федерации|билли\s+джин\s+кинг)|азиатские\s+игры"
    r"|выставочн|парный\s+разряд",
    re.IGNORECASE,
)


def scope(row):
    name = row.get("tournament_name")
    if row.get("gender") not in {"M", "F"} or row.get("level") not in {"tour", "challenger"}:
        return "not_tour_or_challenger"
    if not name:
        return "tournament_unknown"
    if EXCLUDED.search(name) or any(
        "/" in (row.get(side) or "") for side in ("player1", "player2")
    ):
        return "excluded_tournament_or_doubles"
    return None


def latest_details(details, as_of):
    """Take the latest proven details by source timestamp; conflicts at that time stay visible."""
    latest = {}
    for row in details:
        when = instant(row.get("fetched_at"), naive_utc=True)
        if not when or when > as_of:
            continue
        mid = row["canonical_match_id"]
        evidence = (row["nbbet_match_id"], row["status"], row["sets_json"])
        if mid not in latest or when > latest[mid][0]:
            latest[mid] = (when, {evidence})
        elif when == latest[mid][0]:
            latest[mid][1].add(evidence)
    return latest


def project(metadata, details, registry: PinnedIdentities, as_of, *, start=START):
    latest = latest_details(details, as_of)
    counts = Counter()
    player_days = Counter()
    diagnostics = defaultdict(Counter)
    pending = []
    seen = set()
    for row in metadata:
        mid = row["match_id"]
        if mid in seen:
            raise ValueError("Nonunique PostgreSQL match ID")
        seen.add(mid)
        raw_day = row.get("match_date")
        day = date.fromisoformat(raw_day) if raw_day else None
        surface = row.get("surface") if row.get("surface") in SURFACES else None
        reason = None
        if not day or not start <= day <= as_of.date():
            reason = "outside_history_or_missing_date"
        else:
            ids = tuple(r["player_id"] for r in registry.resolve(row, f"public.matches/{mid}"))
            for pid in set(ids) - {None}:
                player_days[pid, day] += 1
            reason = scope(row)
            if reason is None and row.get("status") != 3:
                reason = "not_completed"
            if reason is None and row.get("duplicate_of"):
                reason = "canonical_duplicate"
            if reason is None and (None in ids or ids[0] == ids[1]):
                reason = "unverified_or_equal_players"
            if reason is None:
                outcome = latest.get(mid)
                if outcome is None:
                    reason = "details_missing"
                elif len(outcome[1]) != 1:
                    reason = "conflicting_latest_details"
                else:
                    ((nbbet, status, sets),) = outcome[1]
                    if (
                        type(nbbet) is not int
                        or nbbet <= 0
                        or nbbet != row.get("nbbet_match_id")
                        or status != 3
                    ):
                        reason = "details_status_or_link_mismatch"
                    else:
                        try:
                            score, _ = completed_set_winner(sets, None)
                        except (ContractError, TypeError):
                            reason = "invalid_or_missing_score"
            if reason is None:
                revision = fingerprint([row, outcome[0].isoformat(), nbbet, status, sets])
                pending.append(
                    (mid, day, row["gender"], row["level"], surface, *ids, score, revision)
                )
                reason = "rated"
        counts[reason] += 1
        groups = (
            "all",
            f"year/{day.year if day else 'unknown'}",
            f"gender/{row.get('gender') or 'unknown'}",
            f"level/{row.get('level') or 'unknown'}",
            f"surface/{surface or 'unknown'}",
        )
        for key in groups:
            diagnostics[key][reason] += 1

    matches = []
    target_counts = Counter()
    for mid, day, gender, level, surface, left, right, score, revision in pending:
        if surface is None:
            decision = "unknown_surface"
        elif player_days[left, day] != 1 or player_days[right, day] != 1:
            decision = "nonunique_player_day"
        else:
            decision = "target"
        target_counts[decision] += 1
        for key in (
            "all",
            f"year/{day.year}",
            f"gender/{gender}",
            f"level/{level}",
            f"surface/{surface or 'unknown'}",
        ):
            diagnostics[key][f"target/{decision}"] += 1
        matches.append(
            Match(
                mid,
                day,
                gender,
                level,
                surface,
                left,
                right,
                int(score),
                decision == "target",
                revision,
            )
        )
    return matches, {"results": counts, "targets": target_counts, "segments": diagnostics}
