"""Одноразовый backfill: переносит исторические одиночные матчи и их
поочковые (point-by-point) данные из старой SQLite-базы
(~/tennis_prediction_model_old/tennis_matches.db, таблицы matches_main /
games_stats / points_stats) в новую схему — манифест в Postgres
(tennis_predictor.storage.models.Match) плюс Parquet-файлы в S3.

НЕ для cron / run_ingestion — запускается вручную один раз (при
необходимости — несколько: скрипт идемпотентен, см. ниже).

Что отбрасывается:
  - парные матчи (в player1_name/player2_name встречается "/");
  - всё, чего нет в LEVEL_MAP (юниоры, командные турниры, миксты) —
    это не единичные ATP/WTA/challenger/ITF матчи;
  - строки без match_timestamp (нечем определить дату надёжно);
  - exhibition-матчи (EXHIBITION Мужчины/Женщины) — систематически
    неполные поочковые данные (60-62% брака против 1-4% у
    tour/challenger/itf, см. docs/decisions.md), не репрезентативны
    для модели.

Идемпотентность: canonical_match_id — не случайный uuid4, а
uuid5(namespace, nbbet_match_id), то есть при повторном запуске для
одного и того же исходного матча получается тот же самый UUID. Это
значит, что группу (gender, level, year) можно спокойно переобработать
(например, после сбоя) — Parquet-файл перезапишется идентичным по
уже перенесённым матчам содержимым, а строки, которых ещё нет в
Postgres (по nbbet_match_id), будут доставлены.

Использование:
    uv run python -m tennis_predictor.pipeline.backfill_historical_pbp \\
        [--source-db ~/tennis_prediction_model_old/tennis_matches.db] \\
        [--dry-run] [--only-gender F] [--only-level tour] [--only-year 2023]

--dry-run считает и логирует всё как обычно, но не пишет ни в S3, ни в
Postgres — им имеет смысл сначала прогнать весь скрипт целиком.
"""

import argparse
import io
import logging
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import insert, select

from tennis_predictor.config import get_settings
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.models import Match
from tennis_predictor.storage.s3_client import get_s3_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_SOURCE_DB = Path.home() / "tennis_prediction_model_old" / "tennis_matches.db"

# Префикс tournament_name (часть до первой точки) -> (gender, level).
# Всё остальное (Юноши, Девушки, Команды -*, Смешанные пары,
# EXHIBITION Микст/Мужчины/Женщины) — пропускаем целиком: exhibition
# исключён отдельно как систематически неполный источник (см. docstring).
LEVEL_MAP: dict[str, tuple[str, str]] = {
    "ATP": ("M", "tour"),
    "WTA": ("F", "tour"),
    "ATP Челленджер": ("M", "challenger"),
    "WTA Челленджер": ("F", "challenger"),
    "ITF Мужчины": ("M", "itf"),
    "ITF Женщины": ("F", "itf"),
}

# Колонки итогового Parquet-файла (один файл на связку gender/level/year).
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
]

SQL_CHUNK_SIZE = 500  # лимит sqlite на количество '?' в одном запросе — 999

# Фиксированное пространство имён для детерминированных UUID: один и тот
# же nbbet_match_id всегда даёт один и тот же canonical_match_id, даже
# при повторном запуске скрипта в другой день.
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "tennis-predictor/nbbet-match-id")


def _canonical_match_id(nbbet_match_id: int) -> uuid.UUID:
    return uuid.uuid5(_UUID_NAMESPACE, str(nbbet_match_id))


def _tournament_prefix(tournament_name: str | None) -> str | None:
    """То же самое, что SUBSTR(tournament_name, 1, INSTR(tournament_name, '.') - 1)."""
    if not tournament_name or "." not in tournament_name:
        return None
    return tournament_name.split(".", 1)[0].strip()


def _is_doubles(player_name: str | None) -> bool:
    return bool(player_name) and "/" in player_name


def _chunked(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


@dataclass
class Stats:
    """Счётчики для итогового лога по одной группе (gender, level, year)."""

    matches_inserted: int = 0
    matches_already_migrated: int = 0
    matches_without_points: int = 0
    points_written: int = 0


def load_manifest(sqlite_conn: sqlite3.Connection) -> tuple[pd.DataFrame, dict[str, int]]:
    """Строит манифест матчей-кандидатов из matches_main + matches.

    match_timestamp (Unix мс) берём из matches, а не парсим текстовый
    matches_main.match_date — обе таблицы джойнятся по match_url
    (уникален в обеих и связывает "сырой" сбор с поочковым).

    Возвращает (отфильтрованный DataFrame, счётчики пропусков по причине).
    """
    raw = pd.read_sql_query(
        """
        SELECT
            mm.match_id        AS nbbet_match_id,
            mm.tournament_name AS tournament_name,
            mm.player1_name    AS player1,
            mm.player2_name    AS player2,
            m.match_timestamp  AS match_timestamp
        FROM matches_main mm
        LEFT JOIN matches m ON m.match_url = mm.match_url
        """,
        sqlite_conn,
    )

    expected_count = pd.read_sql_query("SELECT COUNT(*) as c FROM matches_main", sqlite_conn)["c"].iloc[0]
    if len(raw) != expected_count:
        logger.warning(
            "load_manifest: matches_main содержит %d строк, а join дал %d — расхождение %d",
            expected_count, len(raw), expected_count - len(raw),
        )

    skip_counts = {"doubles": 0, "unmapped_level": 0, "missing_timestamp": 0}
    keep_mask: list[bool] = []
    genders: list[str | None] = []
    levels: list[str | None] = []
    match_dates: list[object] = []
    years: list[int | None] = []

    for row in raw.itertuples(index=False):
        if _is_doubles(row.player1) or _is_doubles(row.player2):
            skip_counts["doubles"] += 1
            keep_mask.append(False)
            genders.append(None)
            levels.append(None)
            match_dates.append(None)
            years.append(None)
            continue

        level_info = LEVEL_MAP.get(_tournament_prefix(row.tournament_name))
        if level_info is None:
            skip_counts["unmapped_level"] += 1
            keep_mask.append(False)
            genders.append(None)
            levels.append(None)
            match_dates.append(None)
            years.append(None)
            continue

        if pd.isna(row.match_timestamp) or not row.match_timestamp:
            # LEFT JOIN может дать NaN (не None) для строк без пары в matches —
            # `not nan` в Python равно False, поэтому проверяем pd.isna() отдельно.
            skip_counts["missing_timestamp"] += 1
            keep_mask.append(False)
            genders.append(None)
            levels.append(None)
            match_dates.append(None)
            years.append(None)
            continue

        dt = datetime.fromtimestamp(row.match_timestamp / 1000, tz=UTC)
        gender, level = level_info
        keep_mask.append(True)
        genders.append(gender)
        levels.append(level)
        match_dates.append(dt.date())
        years.append(dt.year)

    raw["gender"] = genders
    raw["level"] = levels
    raw["match_date"] = match_dates
    raw["year"] = years

    manifest = raw[pd.Series(keep_mask)].copy()
    manifest["canonical_match_id"] = manifest["nbbet_match_id"].astype(int).map(_canonical_match_id)
    return manifest, skip_counts


def load_existing_nbbet_ids(conn) -> set[int]:
    """nbbet_match_id, уже присутствующие в Postgres — для идемпотентности."""
    result = conn.execute(select(Match.nbbet_match_id).where(Match.nbbet_match_id.is_not(None)))
    return {row[0] for row in result}


def fetch_pbp(sqlite_conn: sqlite3.Connection, nbbet_ids: list[int]) -> pd.DataFrame:
    """Тянет games_stats JOIN points_stats для списка nbbet-ID одной группы.

    Матчи, у которых есть games_stats, но нет ни одной строки в
    points_stats (неполный исторический сбор), просто не дадут строк —
    это ожидаемо для небольшой доли матчей.
    """
    frames = []
    for chunk in _chunked(nbbet_ids, SQL_CHUNK_SIZE):
        placeholders = ",".join("?" * len(chunk))
        df = pd.read_sql_query(
            f"""
            SELECT
                p.match_id, p.set_num, p.game_num, p.point_num,
                p.score_team1, p.score_team2, p.server_team1,
                p.winner, p.is_break_point, p.is_match_point, p.point_type,
                g.is_tiebreak AS game_is_tiebreak
            FROM points_stats p
            LEFT JOIN games_stats g
                ON g.match_id = p.match_id
               AND g.set_num = p.set_num
               AND g.game_num = p.game_num
            WHERE p.match_id IN ({placeholders})
            """,
            sqlite_conn,
            params=chunk,
        )
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def build_parquet_frame(
    raw_pbp: pd.DataFrame,
    uuid_by_nbbet_id: dict[int, uuid.UUID],
    gender: str,
    level: str,
) -> pd.DataFrame:
    """Плоская таблица очков в формате, который уходит в Parquet."""
    if raw_pbp.empty:
        return pd.DataFrame(columns=PARQUET_COLUMNS)

    df = raw_pbp.copy()

    # В источнике winner/server_team1 иногда приходили пустой строкой
    # вместо числа (pbp_collector.py: point.get('winner', '')). Такие
    # очки дальше не несём, но считаем, сколько отбросили.
    df["winner"] = pd.to_numeric(df["winner"], errors="coerce")
    df["server_team1"] = pd.to_numeric(df["server_team1"], errors="coerce")
    bad = df["winner"].isna() | df["server_team1"].isna()
    if bad.any():
        logger.warning("build_parquet_frame: отброшено %d очков с некорректным winner/server", int(bad.sum()))
        df = df[~bad]

    is_team1_server = df["server_team1"] == 1

    out = pd.DataFrame(
        {
            "canonical_match_id": df["match_id"].astype(int).map(uuid_by_nbbet_id).astype(str),
            "gender": gender,
            "level": level,
            "surface": None,
            "set_num": df["set_num"],
            "game_num": df["game_num"],
            "point_num": df["point_num"],
            "server": is_team1_server.map({True: 1, False: 2}),
            "point_winner": df["winner"].astype(int),
            "score_server": df["score_team1"].where(is_team1_server, df["score_team2"]),
            "score_returner": df["score_team2"].where(is_team1_server, df["score_team1"]),
            "is_break_point": df["is_break_point"].fillna(0).astype(bool),
            "is_match_point": df["is_match_point"].fillna(0).astype(bool),
            "is_tiebreak": df["game_is_tiebreak"].fillna(0).astype(bool),
            "point_type": df["point_type"],
        }
    )
    out["server_won_point"] = out["server"] == out["point_winner"]
    return out[PARQUET_COLUMNS]


def write_parquet_to_s3(df: pd.DataFrame, s3_client, bucket: str, key: str, dry_run: bool) -> None:
    if dry_run:
        logger.info("[dry-run] пропускаю запись в S3: s3://%s/%s (%d строк)", bucket, key, len(df))
        return
    table = pa.Table.from_pandas(df, preserve_index=False)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    s3_client.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())


def insert_matches(conn, rows: list[dict], dry_run: bool) -> None:
    if not rows:
        return
    if dry_run:
        logger.info("[dry-run] пропускаю вставку %d строк в matches", len(rows))
        return
    conn.execute(insert(Match), rows)


def process_group(
    gender: str,
    level: str,
    year: int,
    group: pd.DataFrame,
    sqlite_conn: sqlite3.Connection,
    existing_nbbet_ids: set[int],
    s3_client,
    bucket: str,
    dry_run: bool,
) -> Stats:
    stats = Stats()

    nbbet_ids = group["nbbet_match_id"].astype(int).tolist()
    already_migrated_mask = group["nbbet_match_id"].astype(int).isin(existing_nbbet_ids)
    to_insert = group[~already_migrated_mask]
    stats.matches_already_migrated = int(already_migrated_mask.sum())

    if to_insert.empty:
        logger.info(
            "gender=%s level=%s year=%s: все %d матчей уже мигрированы, пропускаю группу",
            gender, level, year, len(group),
        )
        return stats

    key = f"pbp/gender={gender}/level={level}/year={year}/part-0000.parquet"

    # Parquet пишем по ВСЕЙ группе (не только по новым матчам): при
    # повторной обработке частично мигрированной группы это не даёт
    # затереть файл неполными данными — canonical_match_id детерминирован,
    # поэтому результат идентичен для уже перенесённых матчей.
    uuid_by_nbbet_id = {int(r.nbbet_match_id): r.canonical_match_id for r in group.itertuples(index=False)}
    raw_pbp = fetch_pbp(sqlite_conn, nbbet_ids)
    parquet_df = build_parquet_frame(raw_pbp, uuid_by_nbbet_id, gender, level)
    stats.points_written = len(parquet_df)

    write_parquet_to_s3(parquet_df, s3_client, bucket, key, dry_run)

    # has_pbp честно отражает, есть ли у матча хоть одна строка в
    # получившемся Parquet — матч мог пройти все фильтры (есть в
    # matches_main), но так и не получить ни одного очка в points_stats
    # (неполный исторический сбор, см. расхождение games_stats/points_stats).
    matched_ids = set(parquet_df["canonical_match_id"].astype(str)) if not parquet_df.empty else set()

    rows = [
        {
            "match_id": r.canonical_match_id,
            "gender": gender,
            "level": level,
            "tournament_name": r.tournament_name,
            "match_date": r.match_date,
            "player1": r.player1,
            "player2": r.player2,
            "surface": None,
            "nbbet_match_id": int(r.nbbet_match_id),
            "championat_match_id": None,
            "has_pbp": str(r.canonical_match_id) in matched_ids,
            "pbp_s3_path": key,
        }
        for r in to_insert.itertuples(index=False)
    ]
    stats.matches_without_points = sum(1 for r in rows if not r["has_pbp"])

    with engine.begin() as conn:
        insert_matches(conn, rows, dry_run)
    stats.matches_inserted = len(rows)

    logger.info(
        "gender=%s level=%s year=%s: вставлено=%d (без очков=%d), уже было=%d, очков записано=%d -> s3://%s/%s",
        gender, level, year, stats.matches_inserted, stats.matches_without_points,
        stats.matches_already_migrated, stats.points_written, bucket, key,
    )
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument(
        "--dry-run", action="store_true", help="Только считает и логирует, ничего не пишет в S3/Postgres"
    )
    parser.add_argument("--only-gender", choices=["M", "F"], default=None)
    parser.add_argument(
        "--only-level", choices=["tour", "challenger", "itf", "exhibition"], default=None
    )
    parser.add_argument("--only-year", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()

    source_db = args.source_db.expanduser().resolve()
    if not source_db.exists():
        raise SystemExit(f"Источник не найден: {source_db}")

    # Открываем исходную SQLite строго на чтение — это чужой исторический
    # архив, скрипт не должен иметь возможности его изменить.
    sqlite_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        manifest, skip_counts = load_manifest(sqlite_conn)
        logger.info(
            "Манифест: %d матчей к переносу; пропущено — парных: %d, вне LEVEL_MAP: %d, без timestamp: %d",
            len(manifest), skip_counts["doubles"], skip_counts["unmapped_level"], skip_counts["missing_timestamp"],
        )

        if args.only_gender:
            manifest = manifest[manifest["gender"] == args.only_gender]
        if args.only_level:
            manifest = manifest[manifest["level"] == args.only_level]
        if args.only_year:
            manifest = manifest[manifest["year"] == args.only_year]

        with engine.connect() as conn:
            existing_nbbet_ids = load_existing_nbbet_ids(conn)
        logger.info("Уже в Postgres: %d матчей", len(existing_nbbet_ids))

        s3_client = get_s3_client()

        totals = Stats()
        groups = manifest.groupby(["gender", "level", "year"], sort=True)
        logger.info("Групп (gender, level, year) к обработке: %d", groups.ngroups)

        for (gender, level, year), group in groups:
            group_stats = process_group(
                gender, level, int(year), group, sqlite_conn,
                existing_nbbet_ids, s3_client, settings.s3_bucket, args.dry_run,
            )
            totals.matches_inserted += group_stats.matches_inserted
            totals.matches_already_migrated += group_stats.matches_already_migrated
            totals.matches_without_points += group_stats.matches_without_points
            totals.points_written += group_stats.points_written

        logger.info(
            "Готово. Всего вставлено матчей: %d (из них без единого очка: %d), уже было: %d, очков записано: %d",
            totals.matches_inserted, totals.matches_without_points,
            totals.matches_already_migrated, totals.points_written,
        )
    finally:
        sqlite_conn.close()


if __name__ == "__main__":
    main()
