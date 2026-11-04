"""Внешняя проверка свежести регулярных джоб nb-bet.

job_runs (ingestion/job_runs.py) не видит джобу, которая перестала
стартовать вообще — сломанный cron, упавший сервер, зависший flock.
check() читает ту же таблицу и внешние ресурсы (диск, Postgres, S3) и
возвращает список проблем; сама check() ничего не чинит и не решает,
куда отправить сообщение — это notify().

notify() сейчас пишет в лог (health.log). Канал оповещения заменяем
позже (Telegram и т.п.) без изменения check() — проверка и доставка
разделены сознательно.
"""

import argparse
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import requests
from sqlalchemy import text

from tennis_predictor.config import get_settings
from tennis_predictor.ingestion.job_runs import track_run
from tennis_predictor.ingestion.nbbet.status import FETCH_DETAILS
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.s3_client import get_s3_client

logger = logging.getLogger(__name__)

HEALTH_LOG = "/var/log/ingestion/health.log"
LOG_DIR = "/var/log/ingestion"

# --- периодические джобы: period (как часто должен появляться ok) и
# installed_at (когда джоба реально заведена — первый job_runs или,
# если его ещё не было, когда поставили cron). installed_at даёт
# джобе грейс-период на свой первый период работы, не считывается
# заново из job_runs при каждом запуске (меняется только руками,
# когда джобу меняют/переустанавливают).
GRACE_FACTOR = 1.3  # проблема, если now - max(последний ok, installed_at) > period * это
# heartbeat_period: как часто джоба, пока ОНА РЕАЛЬНО РАБОТАЕТ (status=
# 'running'), обновляет heartbeat_at через job_runs.RunHandle.add() —
# не путать с period (как часто должен появляться НОВЫЙ успешный запуск).
# У detail_job/nbbet-refresh это каждый батч (~200 матчей, 2 потока);
# проверено по логу (nbbet_refresh.log): 15 последовательных батчей,
# разброс 4-5 минут между соседними — отсюда 5 минут с запасом.
HEARTBEAT_GRACE_FACTOR = 3  # порог для running-джобы = heartbeat_period * это
JOBS: dict[str, dict] = {
    "nbbet-sweep": {
        "period": timedelta(hours=3),
        "installed_at": datetime(2026, 10, 6, 8, 22, 38),  # noqa: DTZ001 — naive-UTC, как и остальные datetime-колонки; первый реальный job_runs
    },
    "nbbet-sweep-wide": {
        "period": timedelta(hours=24),
        "installed_at": datetime(2026, 10, 6, 8, 45, 0),  # noqa: DTZ001 — оценка момента добавления --job-name в crontab, job_runs пуст
    },
    "health": {
        "period": timedelta(minutes=15),
        "installed_at": datetime(2026, 10, 6, 14, 31, 50),  # noqa: DTZ001 — naive-UTC; первый реальный job_runs
    },
    "nbbet-refresh": {
        # cron каждые 10 минут; большинство запусков выходят сразу же
        # (status=ok, exit_code=0) из-за затвора count_main_queue — это
        # не повод не считать job_runs ok, затвор отрабатывает штатно.
        # Но когда основная очередь опустела (см. decisions.md), один
        # запуск может идти часами подряд (cron заходит поверх flock -n
        # и просто пропускает тик) — "давно не было ok" тогда ложно
        # считал его сломанным. heartbeat_period покрывает именно этот
        # случай: пока джоба status='running', свежесть мерим по
        # heartbeat_at, а не по времени последнего ЗАВЕРШЁННОГО запуска.
        "period": timedelta(minutes=10),
        "installed_at": datetime(2026, 10, 6, 20, 40, 0),  # noqa: DTZ001 — оценка момента установки cron, job_runs пуст
        "heartbeat_period": timedelta(minutes=5),
    },
}

DETAIL_HEARTBEAT_MAX_AGE = timedelta(minutes=30)  # nbbet-detail: нет свежего heartbeat
DETAIL_THROTTLE_EXIT_CODE = 2  # код выхода при "слишком много 403/429"
MIN_FREE_DISK_PERCENT = 15.0  # меньше — проблема
CHECK_PATHS = (LOG_DIR, "/")


@dataclass
class Problem:
    job: str
    kind: str
    message: str


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _last_run(conn, job_name: str, status: str | None = None, now: datetime | None = None):
    now = now or _now()
    q = "SELECT started_at, heartbeat_at, status, exit_code FROM job_runs WHERE job_name = :job"
    params = {"job": job_name}
    if status is not None:
        q += " AND status = :status"
        params["status"] = status
    q += " ORDER BY started_at DESC LIMIT 1"
    return conn.execute(text(q), params).first()


def _check_schedule(conn, problems: list[Problem], now: datetime, job: str) -> None:
    """Сначала: джоба СЕЙЧАС работает (status='running')? Тогда свежесть
    мерим по heartbeat_at с порогом heartbeat_period * HEARTBEAT_GRACE_FACTOR
    — "давно не было ok" тут неприменимо: нормальный долгий запуск (часы)
    без единого завершения раньше ложно считался сломанным (см.
    decisions.md, nbbet-refresh после опустошения основной очереди).

    Иначе — now - max(последний ok, installed_at) > period * GRACE_FACTOR.
    installed_at даёт джобе грейс-период на собственный первый период
    работы — без него только что добавленная в cron джоба (нет ok, потому
    что её период ещё не наступил) всегда выглядела бы сломанной. Если
    last_ok новее installed_at (обычный случай после первого успешного
    запуска), max() берёт его и работает как простой "давно не было ok".
    """
    spec = JOBS[job]
    running = conn.execute(text("""
        SELECT heartbeat_at FROM job_runs WHERE job_name = :job AND status = 'running'
        ORDER BY started_at DESC LIMIT 1
    """), {"job": job}).first()
    if running is not None and "heartbeat_period" in spec:
        age = now - running.heartbeat_at
        threshold = spec["heartbeat_period"] * HEARTBEAT_GRACE_FACTOR
        if age > threshold:
            problems.append(Problem(
                job, "stale_heartbeat",
                f"{job}: идёт сейчас (running), но heartbeat {age.total_seconds()/60:.0f} мин назад "
                f"(порог {threshold})",
            ))
        return  # running и heartbeat свежий — считать "давно не было ok" здесь не нужно

    row = _last_run(conn, job, status="ok", now=now)
    reference = max(row.started_at, spec["installed_at"]) if row is not None else spec["installed_at"]
    age = now - reference
    threshold = spec["period"] * GRACE_FACTOR
    if age > threshold:
        kind = "no_ok_run" if row is None else "stale"
        detail = "ни одного успешного запуска" if row is None else "последний успешный запуск"
        problems.append(Problem(
            job, kind,
            f"{job}: {detail} {age.total_seconds()/3600:.1f}ч назад (порог {threshold})",
        ))


def _check_detail_job(conn, problems: list[Problem], now: datetime) -> None:
    row = conn.execute(text("""
        SELECT heartbeat_at, status, exit_code FROM job_runs
        WHERE job_name = 'nbbet-detail' ORDER BY heartbeat_at DESC LIMIT 1
    """)).first()
    queue_count = conn.execute(text(f"""
        SELECT COUNT(*) FROM matches
        WHERE status IN {tuple(FETCH_DETAILS)} AND details_fetched_at IS NULL
          AND details_attempts < 5 AND duplicate_of IS NULL
          AND level IN ('tour', 'challenger')
    """)).scalar()

    if row is None:
        if queue_count:
            problems.append(Problem("nbbet-detail", "no_run", "nbbet-detail: нет ни одной записи в job_runs, очередь не пуста"))
        return

    if queue_count and (now - row.heartbeat_at) > DETAIL_HEARTBEAT_MAX_AGE:
        problems.append(Problem(
            "nbbet-detail", "stale_heartbeat",
            f"nbbet-detail: heartbeat {((now - row.heartbeat_at).total_seconds()/60):.0f} мин назад, "
            f"очередь не пуста ({queue_count})",
        ))

    last_finished = conn.execute(text("""
        SELECT exit_code FROM job_runs WHERE job_name = 'nbbet-detail' AND status != 'running'
        ORDER BY started_at DESC LIMIT 1
    """)).first()
    if last_finished is not None and last_finished.exit_code == DETAIL_THROTTLE_EXIT_CODE:
        problems.append(Problem(
            "nbbet-detail", "throttled",
            "nbbet-detail: последний запуск завершился кодом 2 (сайт ограничил, 403/429)",
        ))


def _check_repeated_errors(conn, problems: list[Problem]) -> None:
    job_names = [r[0] for r in conn.execute(text("SELECT DISTINCT job_name FROM job_runs"))]
    for job in job_names:
        rows = conn.execute(text("""
            SELECT status FROM job_runs WHERE job_name = :job
            ORDER BY started_at DESC LIMIT 2
        """), {"job": job}).all()
        if len(rows) == 2 and all(r.status == "error" for r in rows):
            problems.append(Problem(job, "repeated_error", f"{job}: последние 2 запуска подряд завершились ошибкой"))


def _check_disk(problems: list[Problem]) -> None:
    for path in CHECK_PATHS:
        try:
            usage = shutil.disk_usage(path)
        except OSError as e:
            problems.append(Problem("disk", "unreadable", f"{path}: не удалось прочитать использование диска: {e!r}"))
            continue
        free_pct = usage.free / usage.total * 100
        if free_pct < MIN_FREE_DISK_PERCENT:
            problems.append(Problem(
                "disk", "low_space",
                f"{path}: свободно {free_pct:.1f}% (порог {MIN_FREE_DISK_PERCENT}%)",
            ))


def _check_postgres(problems: list[Problem]) -> bool:
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:  # noqa: BLE001
        problems.append(Problem("postgres", "unreachable", f"Postgres не отвечает: {e!r}"))
        return False


def _check_s3(problems: list[Problem]) -> bool:
    try:
        s3 = get_s3_client()
        s3.head_bucket(Bucket=get_settings().s3_bucket)
        return True
    except Exception as e:  # noqa: BLE001
        problems.append(Problem("s3", "unreachable", f"S3 не отвечает: {e!r}"))
        return False


def check(now: datetime | None = None) -> list[Problem]:
    now = now or _now()
    problems: list[Problem] = []

    _check_disk(problems)
    pg_ok = _check_postgres(problems)
    _check_s3(problems)

    if not pg_ok:
        # без Postgres остальные проверки (job_runs, очередь) не имеют смысла
        return problems

    with engine.begin() as conn:
        _check_schedule(conn, problems, now, "nbbet-sweep")
        _check_schedule(conn, problems, now, "nbbet-sweep-wide")
        _check_schedule(conn, problems, now, "nbbet-refresh")
        _check_detail_job(conn, problems, now)
        _check_repeated_errors(conn, problems)

    return problems


def notify(text_: str) -> None:
    """Канал оповещения. Сейчас — лог; заменяется без изменения check()."""
    try:
        with open(HEALTH_LOG, "a") as f:
            f.write(f"{_now().isoformat()} {text_}\n")
    except OSError:
        logger.warning("health: не удалось записать в %s", HEALTH_LOG, exc_info=True)


def _ping_healthcheck() -> None:
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        requests.get(url, timeout=10)
    except requests.RequestException:
        logger.warning("health: не удалось дойти до HEALTHCHECK_URL", exc_info=True)


def run_check() -> int:
    with track_run("health") as h:
        problems = check()
        if problems:
            for p in problems:
                notify(f"[{p.job}/{p.kind}] {p.message}")
            h.add(errors=len(problems), error_sample=problems[0].message)
            h.exit_code = 1
        else:
            _ping_healthcheck()
            h.exit_code = 0
    for p in problems:
        print(f"[{p.job}/{p.kind}] {p.message}")
    if not problems:
        print("ok: проблем не найдено")
    return h.exit_code


def _format_age(now: datetime, started_at: datetime | None) -> str:
    if started_at is None:
        return "нет запусков"
    delta = now - started_at
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f}ч назад"


def run_summary() -> int:
    now = _now()
    lines = ["=== Ежедневная сводка nb-bet ===", now.isoformat()]

    with engine.begin() as conn:
        queue_count = conn.execute(text(f"""
            SELECT COUNT(*) FROM matches
            WHERE status IN {tuple(FETCH_DETAILS)} AND details_fetched_at IS NULL
              AND details_attempts < 5 AND duplicate_of IS NULL AND level IN ('tour', 'challenger')
        """)).scalar()
        # detail_job работает одним долгим процессом (часы/сутки), не
        # перезапускается каждый час — поэтому скорость считаем как
        # среднюю с начала ТЕКУЩЕГО запуска (started_at), а не за
        # последний час: отдельной записи "запущен час назад" может и
        # не быть.
        recent = conn.execute(text("""
            SELECT started_at, heartbeat_at, updated FROM job_runs
            WHERE job_name = 'nbbet-detail' ORDER BY heartbeat_at DESC LIMIT 1
        """)).first()
        speed_per_hour = None
        if recent is not None and recent.heartbeat_at > recent.started_at:
            elapsed_h = (recent.heartbeat_at - recent.started_at).total_seconds() / 3600
            if elapsed_h > 0:
                speed_per_hour = recent.updated / elapsed_h

        lines.append(f"очередь detail_job: {queue_count} строк")
        lines.append(
            f"скорость (среднее с начала текущего запуска, updated/ч): {speed_per_hour:.0f}"
            if speed_per_hour else "скорость: нет данных"
        )

        yesterday = (now - timedelta(days=1)).date()
        by_status = conn.execute(text("""
            SELECT status, COUNT(*) FROM matches
            WHERE match_date = :d AND level IN ('tour', 'challenger') GROUP BY 1 ORDER BY 1
        """), {"d": yesterday}).all()
        lines.append(f"\nматчи за {yesterday} по статусам:")
        for r in by_status:
            lines.append(f"  status={r[0]}: {r[1]}")

        errors_24h = conn.execute(text("""
            SELECT job_name, COUNT(*) FROM job_runs
            WHERE started_at >= :cutoff AND status = 'error' GROUP BY 1
        """), {"cutoff": now - timedelta(hours=24)}).all()
        lines.append("\nошибки за сутки:")
        if errors_24h:
            for r in errors_24h:
                lines.append(f"  {r[0]}: {r[1]}")
        else:
            lines.append("  нет")

        lines.append("\nстатус и возраст джоб:")
        for job in ("nbbet-sweep", "nbbet-sweep-wide", "nbbet-detail", "health"):
            row = _last_run(conn, job, now=now)
            if row is None:
                lines.append(f"  {job}: нет запусков")
            else:
                lines.append(f"  {job}: {row.status}, {_format_age(now, row.started_at)}")

    text_out = "\n".join(lines)
    print(text_out)
    notify(text_out)
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "summary"])
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.command == "check":
        sys.exit(run_check())
    else:
        sys.exit(run_summary())


if __name__ == "__main__":
    main()
