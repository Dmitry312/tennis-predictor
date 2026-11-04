"""Очки с /v1/page-tennis-events-points -> те же входы, что давали
fetch_pbp()/fetch_games_per_set() из sqlite. Формат повторяет разбор
в старом pbp_collector.py (None -> 'AD', point_type = '')."""

import pandas as pd

POINTS_URL = "https://app.nb-bet.com/v1/page-tennis-events-points/{slug}"


def fetch_points(session, slug: str) -> list | None:
    """Сеты с геймами, либо None, если очков нет окончательно.
    Сетевые ошибки пробрасываются (матч повторится в следующем запуске)."""
    resp = session.get(POINTS_URL.format(slug=slug), timeout=30)
    resp.raise_for_status()
    inner = (resp.json() or {}).get("data") or {}
    if inner.get("errorCode") == 404:
        return None
    sets = inner.get("data") or []
    return sets if any(sets) else None


def live_sets_to_inputs(sets: list, nbbet_id: int) -> tuple[pd.DataFrame, dict]:
    rows, games_per_set = [], {}
    for set_num, games in enumerate(sets, start=1):
        if not games:
            continue
        games_per_set[(nbbet_id, set_num)] = len(games)
        for game_num, game in enumerate(games, start=1):
            tb = 1 if game.get("is_tiebreak") == 1 else 0
            srv = 1 if game.get("is_serve_team1") == 1 else 0
            for point_num, p in enumerate(game.get("points") or [], start=1):
                if not isinstance(p, dict):
                    continue
                s1, s2 = p.get("score_team1"), p.get("score_team2")
                rows.append({
                    "match_id": nbbet_id, "set_num": set_num, "game_num": game_num,
                    "point_num": point_num,
                    "score_team1": "AD" if s1 is None else str(s1),
                    "score_team2": "AD" if s2 is None else str(s2),
                    "is_break_point": p.get("is_break_point", 0),
                    "is_match_point": p.get("is_match_point", 0),
                    "point_type": p.get("type", ""),
                    "game_is_tiebreak": tb, "game_server_team1": srv,
                })
    return pd.DataFrame(rows), games_per_set
