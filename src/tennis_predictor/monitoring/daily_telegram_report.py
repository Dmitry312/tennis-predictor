"""Read-only daily Telegram data coverage report for Tour and Challenger."""
import argparse
import io
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import requests
from sqlalchemy import text

from tennis_predictor.config import get_settings
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.s3_client import get_s3_client

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


def stats_ids(target_ids, year):
    if not target_ids:
        return set()
    s3 = get_s3_client()
    bucket = get_settings().s3_bucket
    found = set()
    paginator = s3.get_paginator("list_objects_v2")
    for level in LEVELS:
        for gender in ("M", "F"):
            prefix = f"nbbet_stats/gender={gender}/level={level}/year={year}/"
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith(".parquet"):
                        continue
                    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                    tab = pq.read_table(io.BytesIO(raw), columns=["canonical_match_id"])
                    found.update(str(x) for x in tab.column(0).to_pylist()
                                 if x is not None and str(x) in target_ids)
                    if found == target_ids:
                        return found
    return found


def make_report(day):
    rows = match_rows(day)
    ids = {r["id"] for r in rows}
    stats = stats_ids(ids, day.year)
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
        f"Статистика S3: {len(stats)}/{n} ({pct(len(stats))})\n"
        f"Покрытие: {surface}/{n} ({pct(surface)})\n"
        f"Без pBp: {n - pbp}; без деталей: {n - details}; "
        f"без статистики: {n - len(stats)}\n"
        "ℹ️ Пропуски источника не приравниваются к ошибкам парсера."
    )


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
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
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
