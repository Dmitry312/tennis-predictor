# Historical crosscheck S3 discovery recovery

Incident supplied by owner: production44a8c04, 2026-11-27 09:46:45 UTC, NoSuchKey from load_group/read_rows. Owner reports progress through2026-10-29 inclusive. No production connection or deployment performed in this PR.

## Cause and fix
PBP paths are not authoritative stats pointers. Historical PBP backfill has independent part-0000 files; details writer uses run/batch names and crosscheck appends its own versions. Old load_group invented a sibling GET and ignored other collector files when all matches had PBP paths. Catching404 alone would leave real stats undiscovered.

New load_group lists the entire stats partition on EVERY invocation, never reads PBP paths, and updates a private SQLite row index using key/ETag/size. First encounter downloads each file once (existing32MB compressed object guard retained); subsequent days/process restarts download only new/changed files. Deleted objects are evicted. All rows in an object are indexed, not just today's requested IDs. Queries retrieve requested match/scope rows and preserve existing latest_rows fetched_at semantics. No website re-download caused by cache synchronization itself.

LIST permission/network/limit failure, listed-object404, conditional GET412, GET permission failure and decode failure propagate as fatal infrastructure failures, not missing_stats. The private transaction rolls back the entire group refresh; no day marker is created, so restart retries that date. Only a successfully synchronized full listed inventory can produce missing_stats. Logs include escaped exact object key and structured error code; never credentials or payloads. Listing+GET is not an atomic S3 snapshot; newly appended objects are picked up at next group refresh.

Cache default /var/tmp/tennis-stats-crosscheck-index, private0700, per-bucket SQLite,8MiB SQLite cache. Optional TENNIS_STATS_INDEX_DIR selects another private directory. SQLite serializes refresh writers; existing shared crosscheck flock remains unchanged. Cache is independent of /var/lib/tennis-stats-history marker files; corruption/disk errors stop rather than silently use stale state. A cache-only rebuild can be arranged separately if necessary; do not delete historical markers. Cold indexing still reads a whole year once and may take time; daily repeated full GET scans are eliminated. LIST requests remain necessary to detect appends/replacements immediately. Existing max12000 objects and32MB per-object bounds are retained.

PR98 partial results and manual partial retry queue, PR99 round-the-clock worker and rate limits/resource controls are unchanged. This PR does not include PR102 coverage audit or modify production configuration.

## Already processed dates / targeted replay
Fatal NoSuchKey dates receive no apply/partial marker: resume uses original marker directory and naturally retries first unmarked date (likely2026-10-28 given reported progress, verify journal rather than assume). Do NOT restart from2016.

Successful .apply markers mean each selected match was actually checked against some stored rows, but old source selection could have used an older version when a newer independent collector batch existed. Partial markers may contain false missing_stats for matches present outside PBP siblings. Highest-priority replay queue: existing partial markers containing missing_stats, then dates with multiple independent versions whose latest fetched_at was omitted by old selector. Exact dates/counts are UNKNOWN without reading production markers/index; no affected-date inventory is fabricated. Inspect preserved issues/details plus independent complete-version lookup first. Keep PR102 full coverage separate.

For targeted replay, use a NEW sibling state directory and an explicit narrow date range; original successful/partial markers remain unchanged. First run dry-run to review changes, then apply only after owner approval. Do not copy successes to fresh state or delete originals to force replay. Initial restart of normal worker continues existing checkpoints; targeted replay is a separate explicitly selected operation under the SAME /tmp/nbbet_stats_crosscheck.lock.

## Safe update plan (not executed)
1. Record current service status/journal and last apply/partial dates. Back up /var/lib/tennis-stats-history preserving modes/ownership; record hashes. Do not delete, rename or edit marker files.
2. After approval/merge, deploy the reviewed fix using existing procedure. It adds one pure SQLite module; no migration/dependency required. Do not use PR102 branch.
3. Confirm package includes stats_index.py, runtime account can create private cache directory and sufficient disk is available. Preserve existing service CPUQuota25%, nice/ionice and shared flock. No new cron or morning gate.
4. Restart only the failed historical service after approval. Observe stats_partition_index_ready and precise failing key diagnostics; verify progress continues from first unmarked day and existing marker hashes unchanged. Do not run simultaneous manual apply outside shared lock.
5. Rollback code to previous deployed revision if needed, preserve all markers and source S3 objects. Cache is derived local state, not the authoritative progress ledger.

Example targeted dry-run AFTER update, only for an evidence-confirmed single affected date:
```sh
cd /root/Tennis_pelmen_model
flock -n /tmp/nbbet_stats_crosscheck.lock nice -n 15 \
  .venv/bin/python -m tennis_predictor.ingestion.nbbet.stats_crosscheck_backfill \
  --start 2026-10-28 --end 2026-10-28 --max-days 1 --max-matches 1500 --pause 2 \
  --state-dir /var/lib/tennis-stats-history-recheck-discovery
```
Date is illustrative, not confirmed affected-day evidence. No --apply shown; do not execute this while normal worker holds the lock. This documentation does not authorize production update/replay beyond the user's confirmation requirements.
