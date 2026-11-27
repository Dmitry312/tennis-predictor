# Full Tour/Challenger statistics coverage — PR #102

## Execution status

**NOT RUN against production. No quantitative coverage results are claimed.**

Owner authorization: [expanded task](https://github.com/Dmitry312/tennis-predictor/pull/102#issuecomment-7219042398) explicitly permits a full read-only pass, superseding the original sample limit. Workspace execution is unavailable in this chat. The connected GitHub tools can edit files/run PR CI, but expose no workflow_dispatch operation or SSH execution; existing Actions Secrets cannot be read by the agent. These are execution blockers, not missing owner approval. No deployment/merge or production workflow was started.

No fabricated zero coverage/estimated counts are published as actual measurements. Machine-readable status is in execution-status.json, with empty observations.csv. Run the command below to obtain actual coverage.json/CSV/Markdown, then commit only report outputs (never checkpoint or credentials).

## Method and denominators

- Fixed REPEATABLE READ + READ ONLY PostgreSQL snapshot, named cursor/fetch512, transaction/session read-only verified. Canonical duplicate_of=NULL, status3; status9 separately, gender M/F, level tour/challenger, match_date>=2016. No PBP requirement. All12 control IDs fetched in the same snapshot even if ineligible; controls never silently change denominator.
- All matching S3 year partitions found by four category prefixes for stats and details. Every recognized Parquet object included, including historical part-0000, collector batches and crosscheck files, regardless of PBP path. Unknown layout aborts complete-inventory claim.
- Stats latest row per (canonical_match_id,scope) using normalized fetched_at; absent timestamp sorts oldest, matching current source contract. Tied latest versions with different metrics are CONFLICT/unknown rather than order-dependent. Older rows retained in private SQLite for provenance/version counts; each eligible match counted once. Corrections are read, never generated.
- Details scan projects canonical_match_id/fetched_at/sets_json to derive observed expected set count from latest selected array. Missing/conflicting details means expected sets unknown. Set scope completeness requires exact1..N, not just any set row. Retirement denominator is separate and does not imply completed final set.
- Optional --presence scans raw/PBP ID columns without raw payload. Default stats+details avoids unnecessary PBP scans; raw/PBP physical presence remains unknown, PG flags are labeled flags.
- Any incomplete stats object taints latest-version certainty across ALL groups: a mispartitioned object might contain any ID. Observed rows remain lower bounds, absent is only emitted after complete stable stats inventory. An incomplete details scan prevents proof of expected set coverage.
- Inventory is frozen for each pass; ETag+size are checkpointed, GET uses IfMatch, byte count/ETag verified; final re-list must equal starting manifest. Concurrent additions from crosscheck cause incomplete report, not a false snapshot. Resume reconciles changed/deleted files, retains complete unchanged objects and updates the index. PG denominator remains original snapshot; use new state directory for a fresh denominator. No atomic PG+S3 snapshot is claimed.
- Reports group by year×gender×level×status, additionally surface/tournament (NULL kept), plus overall. Numerators: any row, scope0, any non-null scope0 metric, core scope0, strict28 scope0, any set, exact sets, full/partial/absent/unknown. Percentages carry denominator and completeness.
- 28 fields each reported for match/set scope. Zero is a value; NULL/NaN missing. Match field denominator all eligible matches; set field denominator observed latest set scopes. Expected-set percentages only when expected denominator is fully known. Latest-version observations in incomplete scans are NOT final field coverage.

## Full definition

Core: both players' aces, double faults, first_serve_points_all/won, second_serve_points_all/won, service_games_all/won (16 fields). These counters define a fixed documented engineering baseline, not source-contract truth. Full core match requires proven latest versions, core scope0 and core in every exactly expected set scope. Missing expected set count is partial/unassessed set coverage, not full.

BP won/all, winners, unforced errors and maxima can systematically be absent. They are not silently dropped from the audit: strict all28 completeness and individual field non-null rates remain separate. No adaptive definition that changes per match or masks bad cohorts. Values are not validated against the website or corrected; existing stats_crosscheck owns that task.

## Resource and production isolation

Read runtime health.py as AST, not import, extract MIN_FREE_DISK_PERCENT. Repository value15%; audit stops with5 percentage points extra headroom and minimum512MiB free reserve. Verify actual runtime monitoring below; no claim external host monitoring is configured identically. Repository health.py declares no CPU/RAM alert thresholds. Record hashes of runtime health, installed cron and history service; service active states and known collector counts without command-line/secret output.

Single worker, nice19, ionice idle in launcher; CPU duty target10%, load pause at0.7 per CPU, available RAM reserve max512MiB/25% total, RSS stop min768MiB/20% total, address-space ceiling2GiB. These are conservative audit guardrails, not asserted monitoring thresholds. Actual preflight RAM/CPU/load/disk and resources recorded on execution. If external monitor thresholds are stricter, do not run unchanged; align guardrails first. The owner-authorized run must not proceed without checking actual thresholds.

At pressure: pause5s/recheck, max120s then safe stop/checkpoint, no service manipulation. No production locks acquired; only private audit lock. CPU pacing between batch operations,2MiB/s download ceiling, retries disabled, sequential GET, 1MiB chunks to disk, projected columns/row-group decode guard and iter_batches128/use_threads=False. One whole compressed object is spooled to private disk, never whole memory; oversized projected groups are marked unread, not sampled as complete. If a single group is too large under guardrails, future runner-side decoding with safe credentials delegation/alternative bounded decoder is needed; do not raise production limits blindly.

SQLite cache8MiB, object transactions: crash/interruption rolls back current object, next explicit resume retries it; completed objects not downloaded again. Inventory re-list is intentional verification, not uncontrolled GET retry. No cap on total number of files/years; a 5.5h operational session deadline checkpoints, resume same command to finish logical operation. Last report remains explicitly incomplete until final completion. Disk/WAL resources still monitored; checkpoint must not be committed.

Potential risks: large row group allocation may hit existing hard cap before Python can inspect; unknown objects remain unknown. Changes during long scans can force another pass. SQL snapshot fetch pause can exceed idle timeout and safely abort; re-run before PG snapshot commit starts fresh. Once snapshot committed, resume reuses it. Latest fetched_at is source receipt ordering, not source publication time. Tournament names are preserved for grouping, but names alone do not prove distinct edition identity.

## Source root cause / boundaries

Confirmed exporter limitation: ML source_manifest derives sibling filename from pbp_s3_path; historical PBP filename is not an authoritative stats/details locator. Null PBP path results in no source lookup. This audit removes that dependency by scanning each category once, with disk-backed reusable version index, no per-match full scan.

Crosscheck also has a source-discovery restriction in stats_crosscheck.load_group: when all matches have PBP pointers it selects derived siblings plus crosscheck files, not every collector batch. This is code evidence of a possible separate blind spot, not proof of missing production statistics or authorization to change crosscheck. Current audit does not import/re-run/reconfigure that module.

No claim source_without_stats from S3 absence: website current state not queried. Missing latest stats after a full scan means not_found_in_scanned_inventory. Parser/saving/source root cause needs compact historical raw diagnostics and job history as subsequent evidence; do not blame parser or source based on object names alone.

## Concrete safe server execution without merge/deploy

Use the existing authorized server runtime; no new users, packages, secrets or services. This launcher writes only a new private /var/tmp directory, not the production checkout. Existing root runtime is reused only because deployed Python/.env live under /root; no sudo/new privileges. Run as the existing deployment account with access; do not widen permissions.

1. Verify host resources and actual monitor thresholds via existing operator tooling. Read-only baseline commands:
   `free -m`, `uptime`, `df -h / /var/log/ingestion /var/tmp`, `systemctl is-active tennis-stats-history.service cron.service`.
   Inspect actual monitoring configuration locally without publishing credentials; repository evidence alone cannot establish all host alert thresholds. Ensure ordinary collectors/crosscheck retain headroom. Do not stop them. The script records additional baseline checks and pauses/stops on resource pressure.
2. Fetch exactly the tested commit via launcher (reference supplied in PR after CI):
   `bash run_stats_coverage.sh <TESTED_COMMIT_SHA>`
   The launcher downloads only the two standalone audit files from public GitHub into a private code directory, verifies matching retrieved commit path, runs existing runtime with -I -B, never executes repo setup scripts. Supply the final tested SHA from the PR, not a mutable branch.
3. Repeat the SAME command after interruption to resume. Do not use --presence unless additional raw/PBP column scanning is useful; stats+details are default. Results: `/var/tmp/tennis-stats-coverage-v1/report/{README.md,coverage.csv,fields.csv,coverage.json}`.
4. Copy these aggregate outputs only, review for secrets, and commit under docs/audits/stats-coverage/<snapshot-date>/. Keep exactly one audit PR. Do not upload .env, checkpoint database, raw source files or service logs.

No GitHub write token reaches production. Existing actions workflows show the same runtime and fingerprint-verified SSH approach, but none currently dispatches this tool. A new workflow cannot be registered/launched through this session's connector; do not repurpose deployment/crosscheck or auto-run production in pull_request CI. Manual server execution above is the minimal fallback explicitly requested by owner.

## Control matches

249197,26041,577961,81415,335934,725766,94721,453599,135037,691300,605600,571806. Production results are pending. No fixture counts are reported as real control evidence. Controls include status1/5/8/9 separately from eligible status3; raw/PBP flags do not prove physical presence. The report explicitly lists IDs absent from the PG snapshot and conflict/unread reasons.
