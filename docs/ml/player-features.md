# Player Feature Engine v1 and E04

E04 starts at the owner-approved E03C baseline commit `7a14691`.
The 14-column Logistic Regression and 9-column CatBoost depth 3 / 200
remain the rating-only controls. Their accepted exploratory losses were
0.6155399814 and 0.6177680519 on 35,441 common targets.
[E03C selection](experiments/E03C-selection.json)

## What the engine reads

For each participant the on-demand API uses one history lookup. Historical
Match Data Store uses the existing player index and reads local shards.
The live adapter uses one indexed query for the active revision before
the requested time, including the metric payload. Both apply one shared
window kernel and stop at 15 eligible results. Live facts and identity
proof must be available strictly before cutoff. Historical research only
assumes delayed availability of validated past dates. The compact
historical experiment uses the existing canonical result parser and
statistics validator, including source-local scope checks, reconciliation
when compatible set statistics are present and exact side alignment.
Its single bounded read-only export projects metadata, NBBet details
and NBBet statistics partitions for 2021–2025. PBP, odds, raw payloads,
per-match remote queries and all 2026 partitions are outside the run.

The engine excludes the prediction UTC day. The existing rating target
gate excludes multi-record player-days, while eligible facts from such
days enter history together after the day finishes. Windows select the
last 5, 10 or 15 distinct eligible matches, with same-day ties ordered
by match ID. A match with missing statistics still occupies a position.
Each metric exposes valid matches, missing matches, denominator, ratio
of sums, observation-weighted mean of per-match ratios and population
standard deviation for at least two valid values. Window span is measured
in UTC calendar days. Zero observations produce a missing value.
No eager features are generated when a result is processed.

The cache keys on schema, source, rating snapshot and target cohort.
The in-process caller must advance the source revision after corrections.
Local experiment snapshots preserve paired vectors in compressed NumPy
files. Historical experiments compute only the requested target vectors
with a bounded per-player replay buffer. Snapshot integrity and approval
remain upstream responsibilities.

## Definitions

- Match win is 0 or 1. Set and game shares come from the legal completed
  score. Keep all scope-zero metrics and reject invalid or ambiguous counters.
- First serve in is first-serve points divided by first- and second-serve
  points together. First, second and total serve won rates use their
  corresponding opportunity counts. Return wins complement the opponent's
  serve numbers. Overall point share pools observed serve and return wins
  only when the required counts exist on both sides.
- Ace and double fault rates use own service points. Hold and break use
  own and opposing service games. Break-point conversion uses own
  opportunities and saves use opposing opportunities minus conversions.
  A zero denominator leaves the value missing.
- Winners and unforced errors are valid counts per observed match, with
  one match as denominator. No unverified event or synthetic point is added.

## Registered E04 comparison

2021 warms up history. Use the two chronological halves of 2023 for inner
comparison with earlier training only. Select before the 2024/25 refits.
The latter period is exploratory. Use the same labels, time cuts and
eligible targets for all feature sets. Report all targets and a matched
subset where both players have at least one common valid advanced feature
in the last-10 window. The report records aggregate and segmented log
loss, Brier, AUC, fractional-tie accuracy, calibration and paired day-block
uncertainty. Until actual validation results arrive, any added value is
unmeasured.

Compare 5/10/15, combined windows, 5-minus-15 contrasts and dispersion.
Use own minus opponent, train-only median imputation, missingness
indicators and scaling for Logistic Regression, and native missing
values for CatBoost. Both train and score paired side orientations.
Run group ablations for results, serve/return, games/pressure and shot
counts. Include L1 and L2 logistic; hold CatBoost at its accepted bounded
configuration for this comparison. Diagnose IV, PSI, coverage and
correlation. Select the simplest inner candidate within .0005 log loss
and report .001 sensitivity. Opponent adjustment, skill ratings,
tournament/surface-specific windows and matchup are reserved for later
experiments.

The protected workflow emits `E04.json`. For the exact retained source:

```bash
uv run --locked --group baseline python scripts/run_player_features.py \
  --source /private/frozen-source --output /private/e04 > /private/E04.json
```

Only aggregate metrics are exported unless source encryption is configured.
Source, registry and per-target vectors stay outside Git. Holdout remains
closed. Record real measured lift and the accepted or rejected recipes
before promoting new feature baselines.
