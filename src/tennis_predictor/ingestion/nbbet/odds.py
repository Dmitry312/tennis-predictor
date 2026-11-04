"""Разбор пары коэффициентов "победа в матче" из листинга/страницы
nb-bet. Формат одинаковый в обоих местах — {"1": k1, "2": k2}, где
1 = первый игрок (см. docs/decisions.md: подтверждено на 30 матчах,
0 расхождений ни по значениям, ни по порядку игроков)."""


def odds_pair(block) -> tuple[float, float] | None:
    """{"1": k1, "2": k2} -> (k1, k2) или None. 1 = первый игрок."""
    if not isinstance(block, dict):
        return None
    try:
        a, b = float(block.get("1")), float(block.get("2"))
    except (TypeError, ValueError):
        return None
    if not (1.0 < a <= 500.0 and 1.0 < b <= 500.0):
        return None  # NaN тоже отсекается
    return round(a, 3), round(b, 3)
