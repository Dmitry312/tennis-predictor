# Pre-11:00 NBBet statistics crosscheck

The job compares all 28 statistics fields (14 types x two players), for every match and set, for completed canonical matches in the selected date window. Default: previous three match dates ending yesterday in Europe/Moscow. All tournament levels included. The 300-match safety budget fails rather than silently truncating.

The pipeline reads PostgreSQL and the latest Parquet statistics from S3, then fetches live NBBet pages. Missing source values or scope mismatches are errors; it never replaces known data with NULL. Missing stored stats are reported and fail the run. A minimum 0.5-second pause and 40-minute default deadline bound the job.

Dry-run is the default: no database or S3 writes. With --apply, changed match/set rows are appended under nbbet_stats/ as new Parquet versions, with the corresponding raw page archived under nbbet_raw/. Historical objects and PBP remain unchanged. Downstream ML readers MUST deduplicate by (canonical_match_id, scope) using latest fetched_at; otherwise corrections cause duplicate rows.

## Tests and safe production preflight

CI runs tests for all 28 fields, match/set changes, the observed Geerts 6/10 to 6/9 case, missing values/scopes, and latest-version selection.

After deploying code to the server, run:

    cd /root/Tennis_pelmen_model
    PYTHONPATH=src .venv/bin/python -m tennis_predictor.ingestion.nbbet.stats_crosscheck --date 2026-11-23 --lookback-days 1

This makes live read-only requests and prints a JSON summary. Only after production write approval and checking all downstream ML readers, run the same command with --apply.

## Cron schedule — NOT installed by this PR

The existing 11:00 Europe/Moscow health summary must remain untouched. On a confirmed UTC cron host, 10:05 Moscow is 07:05 UTC (Moscow has UTC+3 year-round). After approval, install:

    5 7 * * * cd /root/Tennis_pelmen_model && flock -n /tmp/nbbet_stats_crosscheck.lock env PYTHONPATH=src /root/Tennis_pelmen_model/.venv/bin/python -m tennis_predictor.ingestion.nbbet.stats_crosscheck --apply >> /var/log/ingestion/nbbet_stats_crosscheck.log 2>&1

The default 40-minute deadline leaves 15 minutes before the 11:00 job. If it fails, alerting/health integration must flag crosscheck as failed; this PR does not alter the existing health job. Daily full-history rescans cannot be guaranteed within this window: historical backfill needs a separate bounded job.

## Daily status and historical audit

Use stats_crosscheck_daily instead of the bare CLI for scheduled runs. It writes
an atomic dated status JSON consumed by the existing Telegram report, including
running/error/success and dry-run/apply mode. Schedule only after approval.
Use the same flock lock for daily and historical jobs; never run them concurrently.

Historical audit (example dates, adjust to verified coverage):

    flock -n /tmp/nbbet_stats_crosscheck.lock env PYTHONPATH=src .venv/bin/python -m tennis_predictor.ingestion.nbbet.stats_crosscheck_backfill --start 2025-01-01 --end 2026-11-23 --state-dir /var/lib/tennis-stats-history --max-days 7

Repeat until complete. Each successful date has a separate atomic JSON marker;
failed dates stop the batch. Apply markers are separate from audit markers.
Do not enable --apply until audit coverage, source request budget, and downstream
version selection are verified and production writes approved. A daily batch
can exceed the match budget; adjust explicitly rather than silently truncating.
Historical corrections reflect current source knowledge, not necessarily what
was available on the historical prediction date. Preserve original versions.
