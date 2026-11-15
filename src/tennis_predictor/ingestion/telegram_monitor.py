"""Telegram transport for data-quality reports. No collector or DB changes.

Default is preview-only. Sending requires explicit --send and environment credentials.
"""
import argparse
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

MOSCOW = ZoneInfo("Europe/Moscow")
MAX_MESSAGE = 4000


def report_date(now=None):
    return (now or datetime.now(MOSCOW)).astimezone(MOSCOW).date() - timedelta(days=1)


def render_daily(day: date, metrics: dict) -> str:
    """Only show measured metrics; missing data is unknown, not zero."""
    labels = {
        "discovered_matches": "Матчей обнаружено",
        "finished_matches": "Завершённых матчей",
        "details_missing": "Без деталей",
        "stats_review": "Статистика: проверить",
        "pbp_review": "pBp: проверить",
        "source_missing": "Пропущено при обнаружении",
        "delayed_jobs": "Задержки сборщиков",
    }
    lines = [f"🎾 Tennis Data Monitor", f"Отчёт за {day:%d.%m.%Y} (Москва)", ""]
    for key, label in labels.items():
        value = metrics.get(key)
        lines.append(f"{label}: {value if isinstance(value, int) and value >= 0 else 'нет данных'}")
    lines.append("")
    lines.append("Статус: предварительный; показатели без проверки не считаются нулевыми.")
    return "\n".join(lines)


def render_alert(job: str, kind: str, message: str) -> str:
    # Plain text: no HTML/Markdown injection or parse errors.
    safe = lambda s, n: " ".join(str(s).split())[:n]
    return f"⚠️ Tennis Data Monitor\n{safe(job, 80)} / {safe(kind, 80)}\n{safe(message, 900)}"


def send_message(text: str, token: str, chat_id: str, session=None):
    if not token or not chat_id:
        raise ValueError("Telegram credentials are missing")
    if len(text) > MAX_MESSAGE:
        raise ValueError("Message exceeds configured length")
    client = session or requests
    response = client.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=15,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram API rejected message")
    return result.get("result", {}).get("message_id")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-json", help="Existing JSON file with validated metric keys")
    parser.add_argument("--date", help="YYYY-MM-DD, default previous Moscow day")
    parser.add_argument("--send", action="store_true", help="Opt in to real Telegram delivery")
    args = parser.parse_args()
    day = date.fromisoformat(args.date) if args.date else report_date()
    metrics = {}
    if args.metrics_json:
        with open(args.metrics_json, encoding="utf-8") as f:
            metrics = json.load(f)
        if not isinstance(metrics, dict):
            parser.error("metrics JSON must be an object")
    message = render_daily(day, metrics)
    if args.send:
        send_message(message, os.getenv("TELEGRAM_BOT_TOKEN", ""),
                     os.getenv("TELEGRAM_CHAT_ID", ""))
        print("Telegram accepted message")
    else:
        print(message)


if __name__ == "__main__":
    main()
