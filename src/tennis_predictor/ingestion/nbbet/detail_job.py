"""Загрузка деталей матчей nb-bet: страница (статус, счёт по сетам,
статистика) и, если очков ещё нет, поочковые данные.

Очередь: Match со status IN (3, 9, 10), details_fetched_at IS NULL,
details_attempts < max_attempts, details_attempted_at либо NULL, либо
старше retry_after_hours — сначала новые. Матч считается готовым
только после записи в S3 и обновления строки в Postgres, поэтому
остановка и падение в любой момент безопасны (теряется максимум один
батч).

failed (страница 404, ошибка разбора) и retry_later (свежий матч без
очков) не получают details_fetched_at — без счётчика попыток они
перекачивались бы на каждом запуске cron. details_attempts считает
такие попытки; матч, дошедший до max_attempts, из очереди выпадает
навсегда (смотреть отдельно: WHERE details_attempts >= max_attempts
AND details_fetched_at IS NULL — не системная ли причина, например
страница у всего турнира 404).

Дубликаты в S3 возможны, если процесс упал между записью в S3 и
обновлением Postgres. Читатели дедуплицируют по ключу матча и берут
файл с большим именем part-{run_id}-... (run_id растёт во времени).
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import bindparam, func, literal, or_, select, tuple_, update

from tennis_predictor.config import get_settings
from tennis_predictor.ingestion.http import make_session
from tennis_predictor.ingestion.job_runs import RunHandle, track_run
from tennis_predictor.ingestion.nbbet.details import (
    details_to_rows,
    fetch_match_page,
    parse_match_page,
)
from tennis_predictor.ingestion.nbbet.discovery import HEADERS
from tennis_predictor.ingestion.nbbet.pbp_frames import build_parquet_frame, write_parquet_to_s3
from tennis_predictor.ingestion.nbbet.pbp_live import fetch_points, live_sets_to_inputs
from tennis_predictor.ingestion.nbbet.status import FETCH_DETAILS
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.models import Match
from tennis_predictor.storage.s3_client import get_s3_client

logger = logging.getLogger(__name__)

RECENT_DAYS = 3        # очки свежего матча могут появиться позже
THROTTLE_SLEEP = 300   # секунд паузы после срабатывания стопа
MAX_TRIPS = 3          # подряд, затем выход с кодом 2
CURSOR_RESET_SECONDS = 1800   # см. run(): курсор периодически сбрасывается в None


class ThrottleGuard:
    """Срабатывает, если среди последних 200 ответов 403/429 больше 1%
    (и не меньше трёх)."""

    def __init__(self, window=200, min_bad=3, ratio=0.01):
        self._codes = deque(maxlen=window)
        self._lock = threading.Lock()
        self.tripped = threading.Event()
        self.min_bad, self.ratio = min_bad, ratio

    def record(self, code: int) -> None:
        with self._lock:
            self._codes.append(code)
            bad = sum(1 for c in self._codes if c in (403, 429))
            if bad >= self.min_bad and bad / len(self._codes) > self.ratio:
                self.tripped.set()

    def reset(self) -> None:
        with self._lock:
            self._codes.clear()
            self.tripped.clear()


@dataclass
class Outcome:
    row: object
    kind: str   # ok | status_changed | retry_later | failed | aborted
    status: int | None = None
    detail_row: dict | None = None
    stat_rows: list | None = None
    raw_row: dict | None = None
    raw_pbp: pd.DataFrame | None = None
    games_per_set: dict | None = None
    odds_open: tuple[float, float] | None = None
    surface: str | None = None
    error: str | None = None


_tls = threading.local()


def _session(guard: ThrottleGuard):
    if not hasattr(_tls, "s"):
        s = make_session(headers=HEADERS, retry_statuses=(500, 502, 503, 504))
        s.hooks["response"].append(lambda r, *a, **k: guard.record(r.status_code))
        _tls.s = s
    return _tls.s


def process_one(r, guard: ThrottleGuard, state: dict, stop: threading.Event) -> Outcome:
    if stop.is_set() or guard.tripped.is_set():
        return Outcome(r, "aborted")
    try:
        s = _session(guard)
        html = fetch_match_page(s, r.nbbet_slug)
        time.sleep(state["pause"])
        if html is None:
            return Outcome(r, "failed", error="страница 404")
        d = parse_match_page(html, r.nbbet_match_id)
        if d.status not in FETCH_DETAILS:
            return Outcome(r, "status_changed", status=d.status)
        out = Outcome(r, "ok", status=d.status)
        out.detail_row, out.stat_rows, out.raw_row = details_to_rows(d, str(r.match_id))
        out.odds_open = d.odds_open
        out.surface = d.surface
        if not r.has_pbp:
            sets = fetch_points(s, r.nbbet_slug)
            time.sleep(state["pause"])
            if sets is not None:
                raw, gps = live_sets_to_inputs(sets, r.nbbet_match_id)
                if not raw.empty:
                    out.raw_pbp, out.games_per_set = raw, gps
            today = datetime.now(tz=UTC).date()
            if out.raw_pbp is None and (today - r.match_date).days <= RECENT_DAYS:
                return Outcome(r, "retry_later")
        return out
    except Exception as e:  # noqa: BLE001
        return Outcome(r, "failed", error=repr(e))


def completeness(frame: pd.DataFrame, gps: dict, nbbet_id: int) -> float | None:
    total = sum(n for (mid, _s), n in gps.items() if mid == nbbet_id)
    if frame.empty or not total:
        return None
    done = frame[frame["game_complete"]].drop_duplicates(["set_num", "game_num"]).shape[0]
    return done / total


def flush(outcomes, run_id, batch_idx, s3, bucket, dry_run) -> None:
    ts = datetime.now(UTC).replace(tzinfo=None)
    ok = [o for o in outcomes if o.kind == "ok"]
    changed = [o for o in outcomes if o.kind == "status_changed"]
    only_details, with_pbp = [], []

    groups: dict[tuple, list[Outcome]] = {}
    for o in ok:
        groups.setdefault((o.row.gender, o.row.level, o.row.match_date.year), []).append(o)

    for (gender, level, year), items in groups.items():
        part = f"gender={gender}/level={level}/year={year}/part-{run_id}-{batch_idx:05d}.parquet"
        pbp_key = f"pbp/{part}"
        det = pd.DataFrame([{**o.detail_row, "fetched_at": ts} for o in items])
        write_parquet_to_s3(det, s3, bucket, f"nbbet_details/{part}", dry_run)
        stats = pd.DataFrame([{**s_, "fetched_at": ts} for o in items for s_ in o.stat_rows])
        if not stats.empty:
            write_parquet_to_s3(stats, s3, bucket, f"nbbet_stats/{part}", dry_run)
        raw = pd.DataFrame([{**o.raw_row, "fetched_at": ts} for o in items if o.raw_row])
        if not raw.empty:
            write_parquet_to_s3(raw, s3, bucket, f"nbbet_raw/{part}", dry_run)

        frames = []
        for o in items:
            r = o.row
            base = {"b_id": r.match_id, "b_status": o.status, "b_ts": ts,
                    "b_version": o.detail_row["details_version"]}
            frame = pd.DataFrame()
            if o.raw_pbp is not None:
                frame = build_parquet_frame(
                    o.raw_pbp, {r.nbbet_match_id: r.match_id}, gender, level, o.games_per_set)
            if frame.empty:
                only_details.append({**base, "b_surface": o.surface})
            else:
                frames.append(frame)
                with_pbp.append({**base, "b_surface": o.surface, "b_path": pbp_key,
                                 "b_compl": completeness(frame, o.games_per_set, r.nbbet_match_id)})
        if frames:
            write_parquet_to_s3(pd.concat(frames, ignore_index=True), s3, bucket, pbp_key, dry_run)

    if dry_run:
        return
    with engine.begin() as conn:
        base_stmt = update(Match).where(Match.match_id == bindparam("b_id"))
        if only_details:
            conn.execute(base_stmt.values(status=bindparam("b_status"),
                                          details_fetched_at=bindparam("b_ts"),
                                          details_version=bindparam("b_version"),
                                          surface=func.coalesce(Match.surface, bindparam("b_surface"))), only_details)
        if with_pbp:
            conn.execute(base_stmt.values(status=bindparam("b_status"),
                                          details_fetched_at=bindparam("b_ts"), has_pbp=True,
                                          pbp_s3_path=bindparam("b_path"),
                                          pbp_completeness=bindparam("b_compl"),
                                          details_version=bindparam("b_version"),
                                          surface=func.coalesce(Match.surface, bindparam("b_surface"))), with_pbp)
        if changed:
            conn.execute(base_stmt.values(status=bindparam("b_status")),
                         [{"b_id": o.row.match_id, "b_status": o.status} for o in changed])
        # Запасной путь: odds_open пишется из sweep (листинг, source=
        # 'listing'), detail_job дозаполняет её из match["30"] страницы
        # ТОЛЬКО если sweep не успел (odds_open_p1 ещё NULL) — тоже
        # "один раз", просто источник другой (source='page').
        odds_rows = [{"b_id": o.row.match_id, "p1": o.odds_open[0], "p2": o.odds_open[1],
                      "b_ts": ts, "b_status": o.status}
                     for o in ok if o.odds_open]
        if odds_rows:
            conn.execute(
                update(Match).where(Match.match_id == bindparam("b_id"), Match.odds_open_p1.is_(None))
                .values(odds_open_p1=bindparam("p1"), odds_open_p2=bindparam("p2"),
                        odds_open_captured_at=bindparam("b_ts"), odds_open_source="page",
                        odds_open_status=bindparam("b_status")),
                odds_rows,
            )
        # failed/retry_later не получают details_fetched_at — без счётчика
        # попыток они перекачивались бы на каждом запуске cron (aborted не
        # считаем попыткой: это остановка по стопу/throttle, матч сам не виноват).
        retry = [{"b_id": o.row.match_id, "b_ts": ts}
                 for o in outcomes if o.kind in ("failed", "retry_later")]
        if retry:
            conn.execute(
                update(Match).where(Match.match_id == bindparam("b_id")).values(
                    details_attempted_at=bindparam("b_ts"),
                    details_attempts=Match.details_attempts + 1),
                retry,
            )


def fetch_pending(conn, levels, cursor, limit, *, retry_after_hours=3, max_attempts=5):
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=retry_after_hours)
    q = select(Match.match_id, Match.nbbet_match_id, Match.nbbet_slug, Match.gender,
               Match.level, Match.match_date, Match.has_pbp).where(
        Match.status.in_(list(FETCH_DETAILS)),
        Match.details_fetched_at.is_(None),
        Match.nbbet_slug.is_not(None),
        Match.level.in_(list(levels)),
        Match.details_attempts < max_attempts,
        or_(Match.details_attempted_at.is_(None), Match.details_attempted_at < cutoff),
        Match.duplicate_of.is_(None),
    )
    if cursor:
        q = q.where(tuple_(Match.match_date, Match.match_id) < tuple_(
            literal(cursor[0], Match.match_date.type), literal(cursor[1], Match.match_id.type)))
    return conn.execute(q.order_by(Match.match_date.desc(), Match.match_id.desc()).limit(limit)).all()


def fetch_stale_version(conn, levels, cursor, limit, target_version):
    """Аналог fetch_pending для --refresh-version: уже обработанные
    строки (details_fetched_at IS NOT NULL), но details_version ниже
    целевой — страницу перекачиваем заново ради новых полей (например,
    рейтинг появился только в DETAILS_VERSION=2). process_one сам не
    трогает очки повторно, если has_pbp уже True."""
    q = select(Match.match_id, Match.nbbet_match_id, Match.nbbet_slug, Match.gender,
               Match.level, Match.match_date, Match.has_pbp).where(
        Match.status.in_(list(FETCH_DETAILS)),
        Match.details_fetched_at.is_not(None),
        Match.details_version < target_version,
        Match.nbbet_slug.is_not(None),
        Match.level.in_(list(levels)),
        Match.duplicate_of.is_(None),
    )
    if cursor:
        q = q.where(tuple_(Match.match_date, Match.match_id) < tuple_(
            literal(cursor[0], Match.match_date.type), literal(cursor[1], Match.match_id.type)))
    return conn.execute(q.order_by(Match.match_date.desc(), Match.match_id.desc()).limit(limit)).all()


def _mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def _load_ok(max_load_ratio: float) -> bool:
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        return True
    cores = os.cpu_count() or 1
    return load1 <= cores * max_load_ratio


def wait_for_resources(
    min_available_mb: int = 400,
    max_load_ratio: float = 0.85,
    check_interval: float = 30,
    max_wait_minutes: float = 10,
) -> bool:
    """Если на сервере мало свободной памяти или высокая загрузка CPU —
    ждём, не забираем ресурсы у чужих процессов (cron гоняет detail_job
    и днём, пока на машине крутятся другие нужные сервисы — см.
    environment-gotchas.md про OOM на этой же 4-ГБ машине). Единственная
    защита на время дневных прогонов.

    Возвращает True, если ресурсы освободились и батч можно запускать.
    False — ресурсы не освободились за max_wait_minutes: вызывающий код
    должен остановиться сам, а не стартовать батч на перегруженной машине
    (раньше после таймаута продолжали запуск — это и была ошибка)."""
    waited = 0.0
    while waited < max_wait_minutes * 60:
        available = _mem_available_mb()
        mem_ok = available is None or available >= min_available_mb
        if mem_ok and _load_ok(max_load_ratio):
            return True
        logger.warning(
            "мало ресурсов (память %sMB/%d, load1 %.2f/лимит %.2f), жду %.0fс",
            available, min_available_mb, os.getloadavg()[0],
            (os.cpu_count() or 1) * max_load_ratio, check_interval,
        )
        time.sleep(check_interval)
        waited += check_interval
    logger.error("ресурсы не освободились за %.0f мин, выходим без запуска батча", max_wait_minutes)
    return False


def count_main_queue(conn, levels, *, retry_after_hours=3, max_attempts=5) -> int:
    """Размер ОСНОВНОЙ очереди (details_fetched_at IS NULL) — те же условия,
    что в fetch_pending, без лимита/курсора. Используется затвором
    --refresh-version: перекачка старых страниц ждёт, пока основная
    очередь не опустеет, чтобы не соревноваться с ней за nb-bet."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=retry_after_hours)
    q = select(func.count()).select_from(Match).where(
        Match.status.in_(list(FETCH_DETAILS)),
        Match.details_fetched_at.is_(None),
        Match.nbbet_slug.is_not(None),
        Match.level.in_(list(levels)),
        Match.details_attempts < max_attempts,
        or_(Match.details_attempted_at.is_(None), Match.details_attempted_at < cutoff),
        Match.duplicate_of.is_(None),
    )
    return conn.execute(q).scalar()


def run(args, h: RunHandle) -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    levels = set(args.levels.split(","))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None
    guard, state = ThrottleGuard(), {"pause": args.pause}
    s3, bucket = get_s3_client(), get_settings().s3_bucket
    totals, trips, processed, cursor, batch_idx = Counter(), 0, 0, None, 0
    cursor_reset_at = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while not stop.is_set():
            if not wait_for_resources():
                logger.info("выходим: ресурсы не освободились")
                return 0
            if deadline and time.monotonic() > deadline:
                logger.info("лимит времени, выходим")
                break
            if args.limit and processed >= args.limit:
                break
            # Без --limit/--max-minutes процесс живёт часами (cron заходит
            # поверх flock -n и просто пропускает запуск, пока старый жив) —
            # курсор тем временем монотонно уходит в прошлое и НИКОГДА не
            # возвращается. Матч, получивший status=3 уже ПОСЛЕ того, как
            # курсор прошёл его match_date, становится недостижим для
            # fetch_pending/fetch_stale_version (условие "< курсор" его не
            # пропустит), пока процесс не перезапустится. Проверено: 53 из
            # 123 (43%) tour/challenger-матчей со status=3 за последние 48ч
            # имели details_fetched_at IS NULL — см. decisions.md. Раз в
            # CURSOR_RESET_SECONDS курсор сбрасывается в None, чтобы
            # очередь заново прошла с самых новых дат.
            if time.monotonic() - cursor_reset_at > CURSOR_RESET_SECONDS:
                logger.info("курсор сброшен (%d мин без сброса) — заново с самых новых дат",
                            CURSOR_RESET_SECONDS // 60)
                cursor = None
                cursor_reset_at = time.monotonic()
            size = min(args.batch_size, args.limit - processed) if args.limit else args.batch_size
            with engine.connect() as conn:
                if args.refresh_version:
                    # Проверяется перед КАЖДЫМ батчем, не только при
                    # старте — если основная очередь наполнилась уже
                    # во время перекачки (sweep нашёл новое), выходим
                    # сразу после того, как уже начатый батч записан
                    # (flush предыдущей итерации), не начиная следующий.
                    pending = count_main_queue(conn, levels)
                    if pending:
                        logger.info(
                            "основная очередь не пуста (%d) — перекачка версии подождёт", pending)
                        break
                    rows = fetch_stale_version(conn, levels, cursor, size, args.refresh_version)
                else:
                    rows = fetch_pending(conn, levels, cursor, size)
            if not rows:
                logger.info("очередь пуста")
                break
            prev_cursor, cursor = cursor, (rows[-1].match_date, rows[-1].match_id)

            outcomes = list(pool.map(lambda r: process_one(r, guard, state, stop), rows))
            flush(outcomes, run_id, batch_idx, s3, bucket, args.dry_run)
            batch_idx += 1
            kinds = Counter(o.kind for o in outcomes)
            totals.update(kinds)
            first_failed = next((o for o in outcomes if o.kind == "failed"), None)
            h.add(
                updated=kinds["ok"],
                skipped=kinds["retry_later"] + kinds["status_changed"],
                errors=kinds["failed"],
                error_sample=first_failed.error if first_failed else None,
            )
            for o in [o for o in outcomes if o.kind == "failed"][:5]:
                logger.warning("failed %s: %s", o.row.nbbet_slug, o.error)
            logger.info("батч %d: %s, пауза %.1fс, всего %s", batch_idx, dict(kinds),
                        state["pause"], dict(totals))

            if guard.tripped.is_set():
                trips += 1
                logger.error("слишком много 403/429 (срабатывание %d из %d)", trips, MAX_TRIPS)
                if trips >= MAX_TRIPS:
                    h.error_sample = "403/429"
                    return 2
                state["pause"] *= 2
                stop.wait(THROTTLE_SLEEP)
                guard.reset()
                cursor = prev_cursor   # вернуться: недоделанные матчи ещё в очереди
            else:
                trips = 0
                processed += len(rows)
    logger.info("итого: %s", dict(totals))
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--levels", default="tour,challenger")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--pause", type=float, default=1.2)
    p.add_argument("--batch-size", type=int, default=300)
    p.add_argument("--max-minutes", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--job-name", default="nbbet-detail")
    p.add_argument(
        "--refresh-version", type=int, default=0,
        help="вместо обычной очереди (details_fetched_at IS NULL) взять уже "
             "обработанные строки с details_version < N и перекачать страницу "
             "заново ради новых полей (0 = обычный режим)",
    )
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with track_run(a.job_name) as h:
        h.exit_code = run(a, h)
    sys.exit(h.exit_code)


if __name__ == "__main__":
    main()
