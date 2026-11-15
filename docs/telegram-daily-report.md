# Daily Telegram coverage report (PostgreSQL-only)

The Telegram reporter is read-only. It does **not** connect to S3, call `health.check()`, change ingestion jobs, or alter existing cron entries.

## Data sources

- PostgreSQL `matches`: Tour/Challenger match counts, pBp, details and surface; exclude `duplicate_of IS NOT NULL`.
- PostgreSQL `job_runs`: last recorded health run, job statuses and errors in the last 24 hours.
- PostgreSQL `matches`: pending detail queue.
- Statistics coverage is explicitly **not checked**: there is no confirmed PostgreSQL field for successfully saved stats. Never infer it from `details_fetched_at`.
- The reporter does not perform an independent S3 health probe. Existing `health check` cron continues to monitor S3.
- Match date comes from source-derived PostgreSQL `match_date`, not a timezone conversion.

## Configuration

Existing private file `/root/Tennis_pelmen_model/.env.telegram` (permissions 0600) supplies `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Environment variables can override these. Never commit secrets.

## Preflight (after code deployment)

```bash
cd /root/Tennis_pelmen_model
PYTHONPATH=src .venv/bin/python -m tennis_predictor.monitoring.daily_telegram_report --date 2026-11-14 --dry-run
```

No Telegram message is sent by `--dry-run`. This performs PostgreSQL reads only.

## One-time report

After reviewing the dry-run output:

```bash
cd /root/Tennis_pelmen_model
PYTHONPATH=src .venv/bin/python -m tennis_predictor.monitoring.daily_telegram_report --date 2026-11-14
```

A successful Telegram API response writes a date-specific marker in `/var/lib/tennis-telegram-report` to prevent accidental resends. Do not use `--force` unless resending deliberately.

## Daily delivery

Existing `health summary` runs at **08:00 UTC = 11:00 Europe/Moscow** (in November). Keep it unchanged. Once validated, add a separate Telegram cron entry at **08:05 UTC** to avoid editing the existing health cron:

```cron
5 8 * * * cd /root/Tennis_pelmen_model && flock -n /tmp/tennis_telegram_daily.lock /root/Tennis_pelmen_model/.venv/bin/python -m tennis_predictor.monitoring.daily_telegram_report >> /var/log/ingestion/telegram_daily.log 2>&1
```

The host cron must be confirmed to run in UTC before adding this line. The Telegram report reads previously recorded health results; it does not call `health summary` or `health check`. The five-minute offset is intentional.

## Release gates

Review and test the new code, run dry-run on server, deploy without modifying existing ingestion cron, send one-time 2026-11-14 report, then install the Telegram cron. Existing GitHub deployment workflow is disabled; this PR does not enable it.
