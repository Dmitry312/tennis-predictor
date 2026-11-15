"""Read-only daily Telegram data coverage report for Tour and Challenger."""
import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import text

from tennis_predictor.storage.db import engine

TZ = ZoneInfo("Europe/Moscow")
LEVELS = ("tour", "challenger")


def match_rows(day):
    # match_date is a source-derived date, not guaranteed to be Moscow-local.
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT match_id::text AS id, level, status, has_pbp,
                   details_fetched_at IS NOT NULL AS has_details,
                   surface IS NOT NULL AS has_surface
            FROM matches
            WHERE match_date = :day
              AND level IN ('tour', 'challenger')
              AND duplicate_of IS NULL
        """), {"day": day}).mappings().all()


def health_snapshot():
    """Read previously recorded health and ingestion results; do not run checks."""
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.connect() as conn:
        queue = conn.execute(text("""
            SELECT COUNT(*) FROM matches
            WHERE status IN (3, 9, 10) AND details_fetched_at IS NULL
              AND details_attempts < 5 AND duplicate_of IS NULL
              AND level IN ('tour', 'challenger')
        """)).scalar()
        errors = conn.execute(text("""
            SELECT job_name, COUNT(*) FROM job_runs
            WHERE started_at >= :cutoff AND status = 'error'
            GROUP BY job_name ORDER BY job_name
        """), {"cutoff": now - timedelta(hours=24)}).all()
        jobs = conn.execute(text("""
            SELECT DISTINCT ON (job_name) job_name, status, heartbeat_at
            FROM job_runs
            WHERE job_name IN ('nbbet-sweep', 'nbbet-sweep-wide',
                               'nbbet-detail', 'nbbet-refresh', 'health')
            ORDER BY job_name, started_at DESC
        """)).all()
    by_job = {name: (status, heartbeat) for name, status, heartbeat in jobs}
    health_row = by_job.get("health")
    if health_row is None:
        health_state = "нет запусков"
    else:
        status, heartbeat = health_row
        age = (now - heartbeat).total_seconds() / 60 if heartbeat else None
        health_state = f"{status}, {age:.0f} мин назад" if age is not None else status
        if age is None or age > 30:
            health_state += " (проверка устарела)"
    lines = [
        "Последняя health-проверка: " + health_state,
        f"Очередь detail_job: {queue}",
        "Ошибки запусков за 24ч: " +
        (", ".join(f"{name}: {n}" for name, n in errors) if errors else "нет"),
        "Джобы: " + "; ".join(
            f"{name}={by_job[name][0]}" if name in by_job else f"{name}=нет запусков"
            for name in ("nbbet-sweep", "nbbet-sweep-wide", "nbbet-detail",
                         "nbbet-refresh", "health")
        ),
    ]
    return "\n".join(lines)


def make_report(day):
    rows = match_rows(day)
    n = len(rows)
    pbp = sum(bool(r["has_pbp"]) for r in rows)
    details = sum(bool(r["has_details"]) for r in rows)
    surface = sum(bool(r["has_surface"]) for r in rows)
    completed = sum(r["status"] in (3, 9, 10) for r in rows)
    def pct(x):
        return f"{x / n * 100:.1f}%" if n else "н/д"
    return (
        f"🎾 Tennis Data Monitor\n"
        f"Дата: {day.isoformat()} (дата матча в PostgreSQL)\n"
        f"Tour / Challenger: {n}\n"
        f"Завершены / retired / awarded: {completed}\n"
        f"pBp: {pbp}/{n} ({pct(pbp)})\n"
        f"Детали: {details}/{n} ({pct(details)})\n"
        "Статистика: не проверяется (нет подтверждённого признака в PostgreSQL)\n"
        f"Покрытие: {surface}/{n} ({pct(surface)})\n"
        f"Без pBp: {n - pbp}; без деталей: {n - details}; "
        f"без покрытия: {n - surface}\n"
        "ℹ️ Пропуски источника не приравниваются к ошибкам парсера.\n\n"
        + health_snapshot()
    )


def telegram_credentials(path="/root/Tennis_pelmen_model/.env.telegram"):
    """Read only the two required values from the existing private config."""
    values = {}
    config = Path(path)
    if config.is_file():
        if config.stat().st_mode & 0o077:
            raise RuntimeError("Telegram config permissions must be 0600")
        for line in config.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
                values[key] = value.strip().strip('"').strip("'")
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or values.get("TELEGRAM_BOT_TOKEN"),
            os.environ.get("TELEGRAM_CHAT_ID") or values.get("TELEGRAM_CHAT_ID"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD; default yesterday in Europe/Moscow")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Resend an already sent date")
    p.add_argument("--state-dir", default="/var/lib/tennis-telegram-report")
    args = p.parse_args()
    day = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else datetime.now(TZ).date() - timedelta(days=1))
    report = make_report(day)
    if args.dry_run:
        print(report)
        return
    token, chat_id = telegram_credentials()
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required")
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / f"{day.isoformat()}.sent"
    if marker.exists() and not args.force:
        print(f"Already sent for {day}")
        return
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": report},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError("Telegram API did not confirm delivery")
    marker.write_text(json.dumps({"date": str(day), "message_id":
                                   payload.get("result", {}).get("message_id")}))
    print(f"Sent report for {day}")


if __name__ == "__main__":
    main()
