"""Обход листингов nb-bet по дням: создаёт и обновляет строки Match
(status, nbbet_slug, start_ts, odds_open_*). Идемпотентен: при конфликте
по nbbet_match_id поля pbp не затрагиваются, odds_open_* пишутся не
более одного раза (см. upsert_candidates).

job_runs.inserted/updated — настоящее число НОВЫХ/ОБНОВЛЁННЫХ строк
Match (через xmax=0 в RETURNING), не "сколько кандидатов обработали" —
это разные вещи: один и тот же уже существующий матч sweep видит на
каждом проходе, но инсертит только один раз."""

import argparse
import json
import logging
import sys
import time
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tennis_predictor.config import get_settings
from tennis_predictor.ingestion.http import make_session
from tennis_predictor.ingestion.job_runs import RunHandle, track_run
from tennis_predictor.ingestion.nbbet.discovery import HEADERS, NbBetCandidate, discover_range
from tennis_predictor.ingestion.nbbet.pbp_frames import write_parquet_to_s3
from tennis_predictor.ingestion.nbbet.pbp_parsing import canonical_match_id
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.models import Match
from tennis_predictor.storage.s3_client import get_s3_client

logger = logging.getLogger(__name__)
BATCH = 2000
ODDS_TOLERANCE = 0.0005


def check_odds_consistency(conn, cands: list[NbBetCandidate]) -> list[dict]:
    """Для кандидатов с odds_open, у которых в БД уже есть значение
    (значит, "первое наблюдение" было раньше) — сравнить с допуском
    ODDS_TOLERANCE. Не меняет данные, только находит расхождения:
    пишем-то мы всё равно только один раз (см. upsert_candidates), это
    просто сигнал, что первое наблюдение было нестабильным."""
    with_odds = {c.nbbet_match_id: c.odds_open for c in cands if c.odds_open is not None}
    if not with_odds:
        return []
    rows = conn.execute(select(Match.nbbet_match_id, Match.odds_open_p1, Match.odds_open_p2).where(
        Match.nbbet_match_id.in_(list(with_odds.keys())), Match.odds_open_p1.is_not(None),
    )).all()
    mismatches = []
    for r in rows:
        new_p1, new_p2 = with_odds[r.nbbet_match_id]
        old_p1, old_p2 = float(r.odds_open_p1), float(r.odds_open_p2)
        if abs(new_p1 - old_p1) > ODDS_TOLERANCE or abs(new_p2 - old_p2) > ODDS_TOLERANCE:
            mismatches.append({
                "nbbet_match_id": r.nbbet_match_id,
                "old": (old_p1, old_p2), "new": (new_p1, new_p2),
            })
    return mismatches


def upsert_candidates(conn, cands: list[NbBetCandidate]) -> tuple[int, int]:
    """(число реально НОВЫХ строк Match, число реально ОБНОВЛЁННЫХ) —
    через системную колонку Postgres xmax: для свежей INSERT-строки она
    0, для строки, затронутой ON CONFLICT DO UPDATE — нет (проверено
    отдельно в транзакции с ROLLBACK). Раньше здесь просто возвращали
    len(cands) и это писалось в job_runs.inserted — то есть "сколько
    кандидатов обработали", а не "сколько новых строк создали": счётчик
    был неверно назван (см. decisions.md, разбор по задаче)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [{
        "match_id": canonical_match_id(c.nbbet_match_id),
        "gender": c.gender, "level": c.level,
        "tournament_name": c.tournament_name, "match_date": c.match_date,
        "player1": c.player1, "player2": c.player2,
        "nbbet_match_id": c.nbbet_match_id, "nbbet_slug": c.slug,
        "status": c.status, "start_ts": c.start_ts, "has_pbp": False, "created_at": now,
        "odds_open_p1": c.odds_open[0] if c.odds_open else None,
        "odds_open_p2": c.odds_open[1] if c.odds_open else None,
        "odds_open_captured_at": now if c.odds_open else None,
        "odds_open_source": "listing" if c.odds_open else None,
        "odds_open_status": c.status if c.odds_open else None,
    } for c in cands]
    # Сортировка по nbbet_match_id — стабильный порядок блокировок строк:
    # теперь два sweep-а (3-часовой и суточный) могут захватывать общие
    # строки почти одновременно, и без единого порядка это был бы риск
    # deadlock между двумя конкурентными UPSERT-транзакциями.
    rows.sort(key=lambda r: r["nbbet_match_id"])
    n_inserted = n_updated = 0
    for i in range(0, len(rows), BATCH):
        stmt = pg_insert(Match).values(rows[i:i + BATCH])
        new = stmt.excluded
        # odds_open_* пишутся ОДИН РАЗ — при первом наблюдении. "fresh":
        # в БД ещё NULL, а в этом проходе цена есть -> пишем; иначе (уже
        # заполнено, или и там и там нет цены) -> оставляем как было.
        fresh = (Match.odds_open_p1.is_(None)) & (new.odds_open_p1.is_not(None))
        stmt = stmt.on_conflict_do_update(
            index_elements=["nbbet_match_id"],
            set_={
                "status": new.status,
                "nbbet_slug": new.nbbet_slug,
                "start_ts": new.start_ts,
                "odds_open_p1": func.coalesce(Match.odds_open_p1, new.odds_open_p1),
                "odds_open_p2": func.coalesce(Match.odds_open_p2, new.odds_open_p2),
                "odds_open_captured_at": case(
                    (fresh, new.odds_open_captured_at), else_=Match.odds_open_captured_at),
                "odds_open_source": case(
                    (fresh, new.odds_open_source), else_=Match.odds_open_source),
                "odds_open_status": case(
                    (fresh, new.odds_open_status), else_=Match.odds_open_status),
            },
        ).returning(sa.literal_column("(xmax = 0)").label("was_insert"))
        for (was_insert,) in conn.execute(stmt):
            if was_insert:
                n_inserted += 1
            else:
                n_updated += 1
    return n_inserted, n_updated


def _odds_row(c: NbBetCandidate, snapshot_at: datetime) -> dict:
    odds5 = c.odds_raw.get("5") if c.odds_raw else None
    odds6 = c.odds_raw.get("6") if c.odds_raw else None
    return {
        "canonical_match_id": canonical_match_id(c.nbbet_match_id),
        "nbbet_match_id": c.nbbet_match_id,
        "snapshot_at": snapshot_at,
        "start_ts": c.start_ts,
        "status": c.status,
        "odds_5_json": json.dumps(odds5, ensure_ascii=False) if odds5 is not None else None,
        "odds_6_json": json.dumps(odds6, ensure_ascii=False) if odds6 is not None else None,
    }


def write_odds_snapshots(odds_rows: dict[int, dict], run_id: str, dry_run: bool) -> int:
    """Один parquet-файл на (run_id, партиция year/month), не на день —
    снимки копятся по всем дням диапазона и пишутся один раз в конце run().
    Дедуп по nbbet_match_id уже сделан на входе (один матч может попасть
    в несколько дней диапазона, см. запись про "догоняющую выборку")."""
    if not odds_rows:
        return 0
    df = pd.DataFrame(odds_rows.values())
    if dry_run:
        logger.info("[dry-run] пропускаю запись nbbet_odds (%d строк)", len(df))
        return len(df)
    s3, bucket = get_s3_client(), get_settings().s3_bucket
    df["_y"] = df["start_ts"].dt.year
    df["_m"] = df["start_ts"].dt.month
    for i, ((y, m), g) in enumerate(df.groupby(["_y", "_m"])):
        key = f"nbbet_odds/year={y}/month={m:02d}/part-{run_id}-{i}.parquet"
        write_parquet_to_s3(g.drop(columns=["_y", "_m"]), s3, bucket, key, dry_run=False)
    return len(df)


def run(start: date, end: date, levels: set[str], pause: float, dry_run: bool, h: RunHandle) -> int:
    session = make_session(headers=HEADERS)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    snapshot_at = datetime.now(UTC).replace(tzinfo=None)
    failed: list[date] = []
    total = 0
    odds_rows: dict[int, dict] = {}
    day = end  # сначала новое
    while day >= start:
        try:
            cands = [c for c in discover_range(day, day, session=session, strict=True)
                     if c.level in levels]
        except Exception as e:  # noqa: BLE001
            logger.error("день %s: %s", day, e)
            failed.append(day)
            h.add(errors=1, error_sample=f"день {day}: {e!r}")
        else:
            if cands and not dry_run:
                with engine.begin() as conn:
                    mismatches = check_odds_consistency(conn, cands)
                    if mismatches:
                        sample = mismatches[:5]
                        logger.warning(
                            "odds_open: %d расхождений с допуском %.4f, примеры: %s",
                            len(mismatches), ODDS_TOLERANCE, sample,
                        )
                        h.add(error_sample=f"odds_open расхождение у {len(mismatches)} матчей: {sample}")
                    n_ins, n_upd = upsert_candidates(conn, cands)
                    h.add(inserted=n_ins, updated=n_upd)
            total += len(cands)
            for c in cands:
                # Снимок в S3 — для ЛЮБОГО статуса, где в листинге есть
                # ключи "5"/"6" (не только 1/2): odds_raw сам по себе не
                # зависит от статуса, и для анализа "что было видно в
                # листинге" интересны все случаи, не только prematch.
                if c.odds_raw is not None:
                    # setdefault: день перебираем от нового к старому,
                    # первое попадание — самый свежий снимок матча.
                    odds_rows.setdefault(c.nbbet_match_id, _odds_row(c, snapshot_at))
            if day.day == 1:
                logger.info("дошли до %s, всего кандидатов %d", day, total)
        time.sleep(pause)
        day -= timedelta(days=1)

    n_odds = write_odds_snapshots(odds_rows, run_id, dry_run)
    if n_odds:
        # НЕ в job_runs.updated — это счётчик для строк Match (см.
        # upsert_candidates), снимки коэффициентов не Match-строки. Раньше
        # сюда писали n_odds, из-за чего updated означал то одно, то другое
        # в разных местах — только лог.
        logger.info("снимков коэффициентов: %d", n_odds)

    logger.info("готово: %d кандидатов, упавших дней: %d", total, len(failed))
    if failed:
        logger.error("повторить вручную: %s", ", ".join(map(str, failed)))
    return 1 if failed else 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=datetime.now(tz=UTC).date())
    p.add_argument("--levels", default="tour,challenger")  # ITF пока не берём
    p.add_argument("--pause", type=float, default=1.2)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--job-name", default="nbbet-sweep")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with track_run(a.job_name) as h:
        h.exit_code = run(a.start, a.end, set(a.levels.split(",")), a.pause, a.dry_run, h)
    sys.exit(h.exit_code)


if __name__ == "__main__":
    main()
