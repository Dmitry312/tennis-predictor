# Daily Telegram coverage report

This is a standalone read-only reporter. It does not modify existing ingestion, cron, migrations, PostgreSQL or S3.

## Configuration
Use existing bot credentials as environment variables (do not commit secrets):
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Preflight
```bash
cd /root/Tennis_pelmen_model
PYTHONPATH=src .venv/bin/python -m tennis_predictor.monitoring.daily_telegram_report --date 2026-11-14 --dry-run
```

## One-time report after release
After deploying the code and verifying the dry run, execute exactly once:
```bash
cd /root/Tennis_pelmen_model
PYTHONPATH=src .venv/bin/python -m tennis_predictor.monitoring.daily_telegram_report --date 2026-11-14
```
This sends only after a successful Telegram API response. A local per-date marker avoids accidental repeat sends. The state directory must be writable by the scheduling user. Do not use `--force` except for an intentional resend.

## Daily schedule: 10:00 Europe/Moscow
Use the existing server scheduler, preferably a separate systemd timer with `OnCalendar=*-*-* 10:00:00`, `Timezone=Europe/Moscow` or set `TZ=Europe/Moscow` in a cron environment with `CRON_TZ=Europe/Moscow` where supported. Verify the actual host scheduler timezone and service user. Do not overwrite ingestion cron. Example on a server whose cron is configured to Europe/Moscow:

```cron
0 10 * * * cd /root/Tennis_pelmen_model && PYTHONPATH=src .venv/bin/python -m tennis_predictor.monitoring.daily_telegram_report >> /var/log/tennis-telegram-report.log 2>&1
```

The report uses PostgreSQL `match_date` and excludes duplicate and ITF records. This date is source-derived and may not match the exact Moscow-local start day. pBp is counted using the PostgreSQL flag; stats availability is verified in S3 parquet. The report measures availability, not ingestion job errors. Empty stats from the provider are not labeled as parser failures.

**Release gates:** review the code and tests, verify bot credentials and chat ID on server, run dry-run, deploy, send the one-time 2026-11-14 report, and then activate the 10:00 schedule. The GitHub deploy workflow currently disables both jobs; this PR does not enable it.
