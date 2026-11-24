# ML foundation — agreed direction

## Architecture
Canonical Parquet layer -> validated match aggregates -> chronological Player State Engine -> versioned Feature Store -> datasets -> training/calibration/backtest and inference. Reuse the same feature functions in replay and inference. Tour/Challenger singles only; odds isolated from model inputs. Keep heavy computation off production where practical. No new production services or database migrations at this stage.

## First audit: player identity and results
PostgreSQL stores player names, not player IDs. details.py preserves match["3"] and match["5"] in players_json and match["24"] in sets_json. Inspect existing details/raw samples for stable source IDs, aliases, collisions, missing IDs, gender consistency and player orientation. Do not assume numeric JSON keys have a particular meaning. Verify across years and levels; sample repeated players across matches. Never merge ambiguous names automatically. Report counts and confidence, not raw personal identifiers in CI logs.

For status 3, derive winner from completed sets after validating score orientation, format and legal terminal score; compare against independent source winner where available. Quarantine conflicts and unknown formats. Status 9 retirement separate; exclude walkovers and awarded matches from baseline target. Audit coverage by year, gender, level, surface and result, including straight-set PBP coverage. Reuse existing bounded read-only audit infrastructure. No production dispatch or writes without authorization.

## Temporal and storage contracts
Separate event time, available_at, ingestion time and prediction cutoff. Strict mode requires verified availability; historical replay uses explicitly documented conservative assumptions and must not be presented as strict. Unknown ranking provenance is excluded from strict features. Immutable manifests pin source object versions or content hashes; checkpoint and replay on historical corrections. Store derived Parquet under ml/ with separate identity, aggregation, state, feature, dataset and model versions. Keep odds separate and join only at backtest time using verified observation timing.

## Experiments and roadmap
Identity/results audit -> canonical layer -> aggregates -> state/checkpoints -> feature store -> dataset builder -> Elo/logistic baseline -> boosting and ablations -> calibration/backtest -> inference. Chronological folds and untouched holdout; log loss primary, Brier/calibration secondary. Candidate features: Elo, rank, form, surface, opponent strength, serve/return, fatigue, pressure, tiebreaks, H2H, style. No mass feature generation until contracts and audit gates are reviewed.

## User-requested ML experiment agent
Explicit project requirement: create a separate ML agent that accepts user hypotheses and wishes, runs reproducible experiments, compares results, maintains an experiment journal and reports findings. Remind the user when Feature Store, Dataset Builder and a reproducible baseline are ready; then design the agent with bounded compute, immutable experiment configurations, holdout protection and no autonomous production deployment. This is a roadmap item, not an active agent or scheduled reminder.
