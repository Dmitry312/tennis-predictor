# ML dataset quality audit — implementation plan

Status: design only. No production query, S3 read, merge or workflow dispatch is authorized by this document.

## Scope and definitions

Evaluate data quality for pre-match tennis prediction, not merely database/S3 uptime. Report **observed**, **eligible**, **excluded** and **unknown** separately; do not label absent PBP as erroneous without determining applicability. Never mutate matches, job_runs, S3, models, or training datasets.

### Match validity
- Counts by calendar year, gender, level, surface, status, and duplicate_of; unknown/NULL in separate buckets.
- NULL dates, future dates relative to audit clock, missing/blank player names, same player on both sides, missing IDs, invalid gender/surface/level.
- Duplicate groups: already marked duplicate_of, dangling/self references, unmarked candidate collisions by normalized (date, players, tournament) **as candidates only**; do not automatically deduplicate or publish player names.
- Outcome availability must be assessed from actual stored schema; do not assume a winner column exists. Separate completed status from independently verified result labels.

### Odds
- Missing pair, one-sided pair, nonfinite/out-of-range values, captured_at missing, capture after start_ts, source/status distribution.
- Odds observed at or after start_ts must be excluded from **pre-match** training; captured_at is observation time, not necessarily true opening price. Do not infer bookmaker.
- Report implied probability and overround only for valid pairs; use bounded aggregates/histograms, never full row exports.

### PBP and statistics
- Coverage by year/status/level; has_pbp, pbp_s3_path, pbp_completeness consistency.
- Invalid completeness outside [0,1], contradictory flags, null/empty paths, duplicate shared batch paths.
- S3 metadata existence checks and full content validation are different. Full Parquet integrity and per-match coverage require a **separately scoped** bounded sampling design with no GetObject by default.
- Determine actual Parquet schema before proposing checks of serves/points/games; no invented DB columns.

### ML leakage / eligibility
- Explicit prediction cutoff: before scheduled start. Compare each feature's event timestamp and observation timestamp against cutoff; unknown provenance => unknown, not safe.
- Avoid target leakage from post-match details, final score, match status, PBP, and retrospective rankings.
- Chronological train/validation/test split with group-aware exclusion of duplicate matches, repeated batch artifacts and near-identical events; embargo if features depend on recent matches.
- Eligibility funnel by year: canonical match -> verified pre-match timestamp -> known label -> available valid pre-match features -> eligible for train/evaluation. Do not report a training-ready count until labels and provenance are verified.

## Safe implementation requirements

1. New standalone `infra/audit/production_ml_quality_audit.py`, new `.github/workflows/production-ml-quality-audit.yml`, new offline tests, update this document. No modifications to existing workflows, application, collectors, cron, Docker, migrations or deployment.
2. Manual workflow_dispatch on main only; pinned checkout; SSH ED25519 fingerprint verified; fixed server/venv path; stdin script; concurrency, 4-minute job timeout, SSH/remote watchdog.
3. PostgreSQL transaction READ ONLY REPEATABLE READ, fixed parameterized SELECT allowlist, explicit schema verification, pg_catalog search_path, 5s statement timeout per query, 1s lock timeout, 5s connect timeout; rollback and close. Query plans reviewed against ~199k matches, avoid unbounded cross joins, sorting all rows, regex on all rows without budget. No arbitrary identifiers or SQL from inputs.
4. Do not import app modules, execute dotenv, use ORM write sessions, write server files, track_run, or read secrets into output. Reuse strict bounded literal config parser (or duplicate a reviewed minimal version); no untrusted exception text, row identifiers, player names, object paths, credentials or raw SQL in logs.
5. Bounded JSON aggregate report (<=16KiB), fixed status/error codes, per-check `complete` flags, explicit denominator and NULL/unknown counts; timeout/incomplete never reported as full.
6. Offline tests for empty DB, null fields, duplicate semantics, missing labels, future timestamps, odds captured after start, year buckets, invalid types, budget/timeouts, redaction, SQL allowlist, no writes. YAML/Bash/Python/actionlint/diff checks.
7. Audit runs only after separate authorization to merge **and** separate authorization to dispatch. Never trigger automatic fixes.

## Implementation sequence

- **Phase A — schema and lineage review:** inspect Match and Parquet schemas, source capture semantics, feature pipeline, result/label availability; finalize bounded SQL checks and eligibility rules.
- **Phase B — implementation and offline verification:** code, tests, docs and Draft PR; review query cost and security.
- **Phase C — authorized production read-only execution:** run once; collect aggregated report and diagnose any incomplete checks.
- **Phase D — recommendations:** issue-ranked quality findings, exact eligible/unknown denominators, proposed data repair or pipeline improvements as separate PRs requiring approval.

## Known schema facts (from repository, not production verification)

`public.matches` includes match_date, start_ts, player1/player2, gender, level, surface, status, duplicate_of, has_pbp, pbp_s3_path, pbp_completeness, odds_open_p1/p2, odds_open_captured_at/source/status. No winner/score column is declared in the current SQLAlchemy Match model. The PBP files may carry outcomes, but this must be verified before declaring training readiness.

Last separate audits reported 199,068 matches and 3,586 distinct PBP paths; these are historical audit observations, not results of this proposed quality audit.

## Implemented PR #11 scope and operational limits

- Standalone SQL audit and manually dispatched SSH workflow; PostgreSQL read-only, repeatable-read, time-limited queries, no raw row identifiers in JSON.
- CI: Python compilation, offline unit tests, and disposable PostgreSQL 16 integration tests (not a production query). CI status must be verified for the latest commit.
- SQL reports aggregate counts, year-level coverage, missing/inconsistent metadata, and **conservative** odds timing checks. It does **not** certify any row as training-ready; winner labels, PBP content, feature provenance and candidate duplicates remain unverified.
- `strict_prematch_odds_finished_canonical` is a preliminary count of records with paired odds observed before `start_ts` and finished status, **not** a count of labeled/eligible training examples.
- `dangling_duplicate` requires a subquery against matches. The production query is capped by statement_timeout, but its plan has not been EXPLAINed on production. If it times out, treat the audit as failed/incomplete; never increase timeout automatically.
- The SQL integration tests use a disposable PostgreSQL instance and synthetic rows. They do not establish production query performance or correctness of upstream data capture.
- `status=3` is finished; `status=9` is finished-retired. The audit keeps them separate. Null and unknown statuses are not assumed finished.
- Only manual workflow_dispatch on `main` can execute the production audit. No merge or dispatch is permitted without separate explicit authorization.
