"""Чистая доменная логика nb-bet: реконструкция очков, канонический ID,
классификация уровня турнира. Без I/O — используется и разовым backfill
(pipeline/backfill_historical_pbp.py), и регулярным коллектором
(ingestion/nbbet/results.py, ingestion/nbbet/pbp.py).
"""

import uuid

LEVEL_MAP: dict[str, tuple[str, str]] = {
    "ATP": ("M", "tour"),
    "WTA": ("F", "tour"),
    "ATP Челленджер": ("M", "challenger"),
    "WTA Челленджер": ("F", "challenger"),
    "ITF Мужчины": ("M", "itf"),
    "ITF Женщины": ("F", "itf"),
}

SCORE_RANK = {"0": 0, "15": 1, "30": 2, "40": 3, "AD": 4, "A": 4}

_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "tennis-predictor/nbbet-match-id")


def canonical_match_id(nbbet_match_id: int) -> uuid.UUID:
    return uuid.uuid5(_UUID_NAMESPACE, str(nbbet_match_id))


def tournament_prefix(tournament_name: str | None) -> str | None:
    if not tournament_name or "." not in tournament_name:
        return None
    return tournament_name.split(".", 1)[0].strip()


def classify_tournament(tournament_name: str | None) -> tuple[str, str] | None:
    """(gender, level) по имени турнира, либо None — не в скоупе модели
    (юниоры/команды/микст/exhibition/нераспознанное)."""
    return LEVEL_MAP.get(tournament_prefix(tournament_name))


def is_doubles(player_name: str | None) -> bool:
    return bool(player_name) and "/" in player_name


def score_rank(value: str | None, tiebreak: bool) -> int | None:
    if tiebreak:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return SCORE_RANK.get(value)


def reconstruct_point_winner(
    prev: tuple[str, str], cur: tuple[str, str], tiebreak: bool
) -> int | None:
    p1, p2 = prev
    c1, c2 = cur
    rp1, rp2 = score_rank(p1, tiebreak), score_rank(p2, tiebreak)
    rc1, rc2 = score_rank(c1, tiebreak), score_rank(c2, tiebreak)
    if None in (rp1, rp2, rc1, rc2):
        return None

    if tiebreak:
        if rc1 > rp1 and rc2 == rp2:
            return 1
        if rc2 > rp2 and rc1 == rp1:
            return 2
        return None

    if rp1 == 4 and rc1 == 3 and rc2 == 3 and rp2 == 3:
        return 2
    if rp2 == 4 and rc2 == 3 and rc1 == 3 and rp1 == 3:
        return 1
    if rp1 == 3 and rp2 == 3 and rc1 == 4 and rc2 == 3:
        return 1
    if rp1 == 3 and rp2 == 3 and rc2 == 4 and rc1 == 3:
        return 2
    if rc1 > rp1 and c2 == p2:
        return 1
    if rc2 > rp2 and c1 == p1:
        return 2
    return None


def is_game_complete(last1: str, last2: str, tiebreak: bool, is_supertiebreak: bool) -> bool:
    if tiebreak:
        try:
            v1, v2 = int(last1), int(last2)
        except (TypeError, ValueError):
            return False
        threshold = 10 if is_supertiebreak else 7
        return abs(v1 - v2) >= 2 and (v1 >= threshold or v2 >= threshold)
    r1, r2 = SCORE_RANK.get(last1), SCORE_RANK.get(last2)
    if r1 is None or r2 is None:
        return False
    return (r1 == 3 and r2 < 3) or (r2 == 3 and r1 < 3) or (r1 == 4 and r2 == 3) or (r2 == 4 and r1 == 3)
