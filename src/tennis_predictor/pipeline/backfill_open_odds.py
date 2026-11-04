"""Разовый backfill: заполняет odds_open_* (Match) из уже скачанного
сырого объекта страницы (nbbet_raw/, match["30"]) — запасной путь для
матчей, которые sweep не застал с ценами в листинге (старый бэклог,
обработанный до появления odds_open, или матчи, у которых цена в
листинге не сохранилась к моменту прохода sweep).

Пишет ТОЛЬКО туда, где odds_open_p1 IS NULL — не перезаписывает то, что
уже установлено sweep'ом/detail_job'ом (правило "один раз, при первом
наблюдении", см. docs/decisions.md). Источник = 'page', captured_at =
fetched_at той самой записи nbbet_raw (не текущее время — мы наблюдаем
цену "задним числом"), status = статус СО СТРАНИЦЫ (match["23"] в
сохранённом сыром объекте), а не текущий Match.status.

По каждому canonical_match_id в nbbet_raw берётся запись с наибольшим
fetched_at (если матч перекачивался несколько раз) — так же, как
читатели parquet дедуплицируют по максимальному fetched_at/run_id
везде в проекте.

Режим по умолчанию — без записи: считает и печатает, сколько строк
обновилось бы (по годам и уровням). --apply пишет на самом деле —
запускать только после того, как число подтверждено отдельно.
"""

import argparse
import json
import logging
import re

import pandas as pd
from sqlalchemy import bindparam, select, update

_PARTITION_RE = re.compile(r"gender=(?P<gender>[^/]+)/level=(?P<level>[^/]+)/year=(?P<year>\d+)/")

from tennis_predictor.config import get_settings
from tennis_predictor.ingestion.nbbet.odds import odds_pair
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.models import Match
from tennis_predictor.storage.s3_client import get_s3_client

logger = logging.getLogger(__name__)


def iter_raw_objects(s3, bucket: str):
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="nbbet_raw/"):
        for obj in page.get("Contents", []):
            yield obj["Key"]


def best_odds_per_match(s3, bucket: str) -> pd.DataFrame:
    """Один проход по всем файлам nbbet_raw/: для каждого
    canonical_match_id держим только запись с максимальным fetched_at —
    raw_json разбирается сразу и не копится в памяти построчно."""
    import io

    import pyarrow.parquet as pq

    best: dict[str, dict] = {}
    n_files = 0
    for key in iter_raw_objects(s3, bucket):
        n_files += 1
        # gender/level/year — не колонки в самом parquet, а Hive-партиции
        # в пути S3-ключа (nbbet_raw/gender=F/level=.../year=.../part-...);
        # читаем файл как есть (без pyarrow dataset-discovery) и достаём
        # их из key регэкспом.
        m = _PARTITION_RE.search(key)
        gender, level, year = (m.group("gender"), m.group("level"), int(m.group("year"))) if m else (None, None, None)
        data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        table = pq.read_table(io.BytesIO(data), columns=[
            "canonical_match_id", "nbbet_match_id", "raw_json", "fetched_at",
        ])
        df = table.to_pandas()
        for row in df.itertuples(index=False):
            prev = best.get(row.canonical_match_id)
            if prev is not None and prev["fetched_at"] >= row.fetched_at:
                continue
            try:
                match = json.loads(row.raw_json)["match"]
            except (TypeError, ValueError, KeyError):
                continue
            pair = odds_pair(match.get("30"))
            best[row.canonical_match_id] = {
                "canonical_match_id": row.canonical_match_id,
                "nbbet_match_id": row.nbbet_match_id,
                "fetched_at": row.fetched_at,
                "gender": gender,
                "level": level,
                "year": year,
                "page_status": match.get("23"),
                "odds_open": pair,
            }
    logger.info("nbbet_raw: %d файлов, %d уникальных матчей", n_files, len(best))
    return pd.DataFrame(best.values())


def rows_to_apply(df: pd.DataFrame) -> pd.DataFrame:
    """Из best_odds_per_match: только те, где цена нашлась, и только
    туда, где в Postgres пока NULL."""
    with_odds = df[df["odds_open"].notna()].copy()
    if with_odds.empty:
        return with_odds
    ids = with_odds["canonical_match_id"].tolist()
    with engine.connect() as conn:
        null_ids = {
            str(r.match_id) for r in conn.execute(
                select(Match.match_id).where(
                    Match.match_id.in_(ids), Match.odds_open_p1.is_(None),
                )
            )
        }
    return with_odds[with_odds["canonical_match_id"].isin(null_ids)]


def summarize(df: pd.DataFrame) -> None:
    if df.empty:
        print("нет строк для обновления")
        return
    g = df.groupby(["year", "level"]).size().reset_index(name="n")
    print(g.to_string(index=False))
    print(f"итого: {len(df)}")


def apply_updates(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = [{
        "b_id": r.canonical_match_id,
        "p1": r.odds_open[0], "p2": r.odds_open[1],
        "b_ts": r.fetched_at, "b_status": r.page_status,
    } for r in df.itertuples(index=False)]
    with engine.begin() as conn:
        conn.execute(
            update(Match).where(Match.match_id == bindparam("b_id"), Match.odds_open_p1.is_(None))
            .values(odds_open_p1=bindparam("p1"), odds_open_p2=bindparam("p2"),
                    odds_open_captured_at=bindparam("b_ts"), odds_open_source="page",
                    odds_open_status=bindparam("b_status")),
            rows,
        )
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="записать (по умолчанию — только посчитать)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    s3, bucket = get_s3_client(), get_settings().s3_bucket
    best = best_odds_per_match(s3, bucket)
    to_apply = rows_to_apply(best)
    summarize(to_apply)

    if a.apply:
        n = apply_updates(to_apply)
        print(f"записано: {n}")
    else:
        print("[без записи] для применения — повторить с --apply")


if __name__ == "__main__":
    main()
