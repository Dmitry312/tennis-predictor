# ML foundation — agreed direction

## Architecture
Canonical Parquet layer -> Aggregation Engine -> Match Data Store -> on-demand Feature Engine -> datasets and inference. A versioned Feature Store may cache requested prematch vectors. Reuse the same feature functions in replay and inference. Tour/Challenger singles only; odds isolated from model inputs. Keep heavy computation off production where practical. No new production services or database migrations at this stage.

## Canonical Data Layer v1

The standalone audit is closed. The [Canonical Data Layer](canonical-layer.md)
imports the repository's real catalog and runs identity, result, coverage,
statistics, PBP, context and timing checks during transformation. It produces
11 typed local Parquet tables and a quality report. Further quality gates belong
in the ML component that consumes the corresponding data.

For a completed NBBet match (status 3), legal set tallies of 2:0 or 2:1
establish a BO3 winner, and 3:0, 3:1 or 3:2 establish a BO5 winner.
For the same NBBet match ID, score sides are the corresponding
`matches.player1` and `matches.player2`. This is the verified source
contract from the [owner's clarification in PR #90](https://github.com/Dmitry312/tennis-predictor/pull/90#issuecomment-7195523871).
Match-local winners, cross-match identity, as-of availability and
training eligibility have separate gates. Escalate new domain ambiguities
to the owner.

## Postmatch processing and historical features

The [Aggregation Engine and Match Data Store](aggregation.md) persist reusable
match and set observations after successful single-match processing or correction.
Owner-confirmed NBBet aggregation uses source-side statistics, HH:MM durations, and compatible opponent counters.
The future Feature Engine reads indexed history and computes only requested
features for prediction or chronological dataset building. Player state,
checkpoints and caches remain optional implementation stages.
Match-local binary targets are independent of longitudinal identity and
publication time. Historical completed-match facts are usable on later UTC
dates under the explicit research convention. Live facts are usable after
successful processing. Dataset builders must exclude same-player multiple-match
days from target eligibility while retaining their facts for later dates.

## Temporal and storage contracts
Separate event time, available_at, ingestion time and prediction cutoff. Strict mode requires verified availability; historical replay uses explicitly documented conservative assumptions and must not be presented as strict. Unknown ranking provenance is excluded from strict features. Immutable manifests pin source object versions or content hashes; checkpoint and replay on historical corrections. Store derived Parquet under ml/ with separate identity, aggregation, state, feature, dataset and model versions. Keep odds separate and join only at backtest time using verified observation timing.

## Experiments and roadmap
Canonical layer -> reusable match facts -> on-demand features -> dataset builder -> Elo/logistic baseline -> boosting and ablations -> calibration/backtest -> inference. Chronological folds and untouched holdout; log loss primary, Brier/calibration secondary. Candidate features: Elo, rank, form, surface, opponent strength, serve/return, fatigue, pressure, tiebreaks, H2H, style. No mass feature generation until contracts and audit gates are reviewed.

## User-requested ML experiment agent
Explicit project requirement: create a separate ML agent that accepts user hypotheses and wishes, runs reproducible experiments, compares results, maintains an experiment journal and reports findings. Remind the user when Feature Store, Dataset Builder and a reproducible baseline are ready; then design the agent with bounded compute, immutable experiment configurations, holdout protection and no autonomous production deployment. This is a roadmap item, not an active agent or scheduled reminder.


## Routine operation convention — owner decision 2026-11-26, Issue #76

Standard operations should run with one button and zero typed technical parameters.
They must inspect current durable state, identify the next bounded step and continue
idempotently. Version defaults and technical settings in workflow/project configuration,
rather than ask the operator to reconstruct them for every run. Optional advanced
overrides are for exceptional cases; they must not become prerequisites for normal use.

Use the selected execution commit automatically and check it out explicitly for
reproducibility. Equality between two variables derived from the same SHA proves
consistency, not independent code review. Do not claim GitHub automatically selected
a branch: selecting the required branch in its UI may still be necessary, and code
must reject wrong-branch execution.

One-click behavior never bypasses existing Environment approval, branch restrictions,
manual merge or explicit authorization for risky operations such as production writes,
schema changes or permission expansion. Existing standing permissions can be recorded
in configuration for a specific bounded operation; they are not new privileges.
Preserve load limits, read-only source access, secret checks, deterministic manifests,
duplicate protection, safe diagnostics and failure-before-publication behavior.

For the standard sample workflow, batch size 5, dispatch SHA and existing root runtime
permission are versioned workflow settings. The persisted plan/catalog determine the
next position. Source-history manifests and plan disambiguation remain optional
advanced inputs. Separate legacy fallback workflows retain their own defaults.


## One logical operation, one final result — Issue #81, 2026-11-26

One user-requested logical operation maps to one manual dispatch and one final
PR/report. Internal batching is implementation detail: run bounded batches
sequentially, infer the next slice from durable state, and do not require the
operator to repeat dispatch/approval/merge for each internal batch. Publish only
after every requested slice validates. No intermediate public branches, partial
catalog commits or automatic merges.

Preserve per-batch read-only limits and provenance while imposing finite aggregate
time, memory, storage and request budgets. Accumulate only in a private workspace.
On failure, publish nothing incomplete, clean temporary bytes and restart from the
last committed prefix when no durable private checkpoint exists. Report limits,
missing source history and snapshot differences honestly. Existing Environment
approval and explicit final merge remain; one-click operation does not broaden
source permissions or authorize production writes.


## Real catalog semantic audit — post PR #86

See [data_audit/README.md](data_audit/README.md) for evidence from all 20 committed real examples, proposed canonical contracts, feature priorities and unresolved coverage/time/identity gates. Run `python scripts/audit_ml_catalog.py` for the deterministic offline JSON report. The canonical ETL implements the reviewed rules and documents its physical contracts and limitations in the [layer guide](canonical-layer.md).
