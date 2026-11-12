# NB-Bet Python collector release (PR #14)

## Deployment target

Production inventory (2026-11-12): `/root/Tennis_pelmen_model`, `.venv/bin/python`, and installed cron entries for `tennis_predictor.ingestion.nbbet.detail_job` and `sweep`. This collector is **not** deployed through the Docker image. The current `.github/workflows/deploy.yml` has disabled jobs and must not be used for this release.

## Preconditions / stop conditions

1. PR #14 is merged into `main`; record its merge commit SHA. Never deploy from a draft/feature branch.
2. Obtain a fresh **read-only** production inventory. Confirm actual Git HEAD, working-tree cleanliness, expected Python files, cron, disk headroom, and any local modifications. Abort on unknown or dirty state; do not use `git reset --hard`, `git clean`, or `git pull` blindly.
3. Verify that `main` contains only reviewed changes intended for release. Validate source commit and test status.
4. Check PostgreSQL and S3 health independently; establish pre-release match/statistics/PBP counts. The release must not run migrations, backfill, or modify PostgreSQL/S3.
5. Verify server SSH ED25519 fingerprint against `SERVER_HOST_FINGERPRINT` before authentication. Never disable host-key verification.
6. Confirm a maintenance window and a tested way to pause the two cron detail jobs (regular and `--refresh-version 3`) without disturbing `sweep` or health checks. Wait for in-flight jobs to finish; do not delete lock files or kill jobs indiscriminately.

## Release approach

**This document is a runbook, not an executable deployment.** Use a separately reviewed, manually dispatched workflow after the preconditions are proven. The deployment must:

- Stage the exact reviewed source tree in a separate release directory (or an immutable wheel/venv) rather than overwriting a live tree.
- Preserve `.env`, existing `.venv`, cron, logs, database volumes, and S3.
- Run `compile()` / import checks in the staged environment and verify that the installed package resolves to the staged source, not an older editable install.
- Take a recoverable snapshot of the previous application source and record its commit; ensure rollback can restore the prior code without touching data.
- Switch the code only while both detail-job cron invocations are paused and in-flight processes have exited. Resume cron after verification.
- Observe at least one normal 10-minute cycle and check `job_runs`, errors, detail/stat/PBP throughput and surface values. Do not infer successful data ingestion from a successful GitHub job alone.
- Stop and roll back application code if imports fail, jobs fail, error rates rise, or match/stat/PBP completeness regresses. Preserve DB/S3 data and report any partial writes.

## Surface-specific limitation

The parser was exercised against eight real NB-Bet pages, all hard; clay/grass are unit-tested but not live-verified. `detail_job` normally selects only rows with `details_fetched_at IS NULL` (and retry limits), so historical surface completeness will not be fixed by deployment alone. Plan historical backfill separately with rate limiting, observability, and explicit approval.

## Explicit non-goals

No merge, production writes, cron changes, database migrations, S3 changes, historical backfill, or automatic deploy are authorized by preparing this runbook.

## Acceptance checklist

- [ ] PR #14 merged and exact release SHA recorded
- [ ] Fresh production inventory and clean working tree confirmed
- [ ] Reviewed manual deployment workflow and rollback tested
- [ ] Both detail-job cron paths safely quiesced for cutover
- [ ] Code updated and correct module import path confirmed
- [ ] At least one 10-minute ingestion cycle verified
- [ ] Match, statistics, PBP and surface metrics compared to baseline
- [ ] Rollback path and release evidence recorded
