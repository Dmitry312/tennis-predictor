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