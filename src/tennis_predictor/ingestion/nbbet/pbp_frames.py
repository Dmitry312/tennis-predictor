"""Сборка Parquet-кадра поочковых данных nb-bet и запись в S3.

Перенесено из pipeline/backfill_historical_pbp.py как есть, без
изменения поведения: раньше backfill — единственный пользователь,
теперь к нему добавляется ingestion/nbbet/detail_job.py (регулярная
догрузка очков с живого API), и зависимость ingestion -> pipeline
была бы в обратную сторону.
"""

import io
import logging
import uuid

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from tennis_predictor.ingestion.nbbet.pbp_parsing import is_game_complete as _is_game_complete
from tennis_predictor.ingestion.nbbet.pbp_parsing import (
    reconstruct_point_winner as _reconstruct_point_winner,
)

logger = logging.getLogger(__name__)

# Колонки итогового Parquet-файла (один файл на связку gender/level/year
# в backfill, на батч в detail_job).
PARQUET_COLUMNS = [
    "canonical_match_id",
    "gender",
    "level",
    "surface",
    "set_num",
    "game_num",
    "point_num",
    "server",
    "point_winner",
    "server_won_point",
    "score_server",
    "score_returner",
    "is_break_point",
    "is_match_point",
    "is_tiebreak",
    "point_type",
    "game_complete",
]


def build_parquet_frame(
    raw_pbp: pd.DataFrame,
    uuid_by_nbbet_id: dict[int, uuid.UUID],
    gender: str,
    level: str,
    games_per_set: dict[tuple[int, int], int],
) -> pd.DataFrame:
    """Плоская таблица очков в формате, который уходит в Parquet.

    winner/server_team1 в points_stats биты (см. SCORE_RANK в pbp_parsing.py), поэтому:
      - point_winner реконструируется по прогрессии score_team1/score_team2
        между соседними очками гейма (_reconstruct_point_winner) — та же
        логика, что провалидирована в диагностике, перенесена как есть;
      - server гейма берём из games_stats.server_team1 (games_per_set/
        raw_pbp содержат game_server_team1) — одно значение на весь
        обычный гейм; для тай-брейка server=None (подача чередуется,
        точную формулу пока не считаем);
      - game_complete — терминально ли последнее очко гейма (win-threshold
        + отрыв ≥2 для тай-брейка, различающиеся ранги для обычного).

    Накопление колоночное (список примитивов на колонку), а не список
    словарей на строку: на боевых объёмах (500К+ очков в одной группе)
    список словарей — это сотни лишних МБ (у каждого dict своя хэш-таблица
    на 17 ключей) и стало причиной OOM на 4-ГБ машине при полном бэкфилле.
    """
    if raw_pbp.empty:
        return pd.DataFrame(columns=PARQUET_COLUMNS)

    unresolved = 0

    col_canonical_match_id: list[str] = []
    col_set_num: list = []
    col_game_num: list = []
    col_point_num: list = []
    col_server: list = []
    col_point_winner: list = []
    col_server_won_point: list = []
    col_score_server: list = []
    col_score_returner: list = []
    col_is_break_point: list = []
    col_is_match_point: list = []
    col_is_tiebreak: list = []
    col_point_type: list = []
    col_game_complete: list = []

    # sort_values(inplace=True) вместо sorted_pbp = raw_pbp.sort_values(...):
    # raw_pbp больше нигде не используется в вызывающем коде, повторная
    # копия всего фрейма ни к чему.
    raw_pbp.sort_values(["match_id", "set_num", "game_num", "point_num"], inplace=True)

    for (match_id, set_num, game_num), game_points in raw_pbp.groupby(
        ["match_id", "set_num", "game_num"], sort=False
    ):
        first = game_points.iloc[0]
        is_tiebreak = bool(first["game_is_tiebreak"]) if pd.notna(first["game_is_tiebreak"]) else False
        game_server_team1 = first["game_server_team1"]

        if is_tiebreak:
            server = None
        elif game_server_team1 == 1:
            server = 1
        elif game_server_team1 == 0:
            server = 2
        else:
            server = None  # games_stats.server_team1 сам не распознан (NaN и т.п.)

        is_supertiebreak = is_tiebreak and games_per_set.get((match_id, set_num), 0) == 1
        canonical_str = str(uuid_by_nbbet_id[int(match_id)])

        prev = ("0", "0")
        last_state = prev
        game_start = len(col_game_complete)
        for point in game_points.itertuples(index=False):
            cur = (point.score_team1, point.score_team2)
            point_winner = _reconstruct_point_winner(prev, cur, is_tiebreak)
            if point_winner is None:
                unresolved += 1

            if server is not None and point_winner is not None:
                server_won_point = server == point_winner
            else:
                server_won_point = None

            if server == 1:
                score_server, score_returner = cur[0], cur[1]
            elif server == 2:
                score_server, score_returner = cur[1], cur[0]
            else:
                score_server, score_returner = None, None

            col_canonical_match_id.append(canonical_str)
            col_set_num.append(set_num)
            col_game_num.append(game_num)
            col_point_num.append(point.point_num)
            col_server.append(server)
            col_point_winner.append(point_winner)
            col_server_won_point.append(server_won_point)
            col_score_server.append(score_server)
            col_score_returner.append(score_returner)
            col_is_break_point.append(bool(point.is_break_point) if pd.notna(point.is_break_point) else False)
            col_is_match_point.append(bool(point.is_match_point) if pd.notna(point.is_match_point) else False)
            col_is_tiebreak.append(is_tiebreak)
            col_point_type.append(point.point_type)
            col_game_complete.append(False)  # проставляется ниже по последнему очку гейма

            prev = cur
            last_state = cur

        game_complete = _is_game_complete(last_state[0], last_state[1], is_tiebreak, is_supertiebreak)
        for i in range(game_start, len(col_game_complete)):
            col_game_complete[i] = game_complete

    if unresolved:
        logger.warning(
            "build_parquet_frame: не удалось реконструировать победителя %d очков "
            "(счёт не изменился между соседними строками, либо скачок вне правил "
            "0-15-30-40-AD/тай-брейка) — это отдельная форма брака, не связанная "
            "с пустыми winner/server_team1",
            unresolved,
        )

    out = pd.DataFrame(
        {
            "canonical_match_id": col_canonical_match_id,
            "gender": gender,
            "level": level,
            "surface": None,
            "set_num": col_set_num,
            "game_num": col_game_num,
            "point_num": col_point_num,
            # Явный nullable-тип сразу при конструировании — без него pandas
            # апкастит колонку с int+None в float64 (None -> NaN), стирая
            # разницу между "0/1/2" и "не определилось".
            "server": pd.array(col_server, dtype="Int64"),
            "point_winner": pd.array(col_point_winner, dtype="Int64"),
            "server_won_point": pd.array(col_server_won_point, dtype="boolean"),
            "score_server": col_score_server,
            "score_returner": col_score_returner,
            "is_break_point": col_is_break_point,
            "is_match_point": col_is_match_point,
            "is_tiebreak": col_is_tiebreak,
            "point_type": col_point_type,
            "game_complete": col_game_complete,
        }
    )
    return out[PARQUET_COLUMNS]


def write_parquet_to_s3(df: pd.DataFrame, s3_client, bucket: str, key: str, dry_run: bool) -> None:
    if dry_run:
        logger.info("[dry-run] пропускаю запись в S3: s3://%s/%s (%d строк)", bucket, key, len(df))
        return
    table = pa.Table.from_pandas(df, preserve_index=False)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    buf.seek(0)
    # Body=buf (файлоподобный объект), а не buf.getvalue() — второе делает
    # ещё одну полную копию байт перед отправкой, лишнее на больших группах.
    s3_client.put_object(Bucket=bucket, Key=key, Body=buf)
