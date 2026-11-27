# Validation protocol

Use all eligible results since 2016 to warm up rating history. A later ML
training window changes dataset sampling, while rating warmup still uses
earlier eligible history. Use rolling-origin validation on earlier periods
and reserve one untouched final chronological holdout. Never choose settings
from holdout results. Freeze the config, versions and splits before opening
the holdout.

Compare models on the identical target population. Exclude a target when
either participant has multiple records on that UTC date. Include eligible
results from those dates in later rating history. Track candidate count,
eligible count, log loss, Brier score, accuracy and calibration-bin counts
by gender, level, surface, newcomer and inactivity. The runner defines
long inactivity as at least 30 UTC days for either participant. Accuracy
gives half credit for exact ties. Report coverage relative to valid rated
results and show source match counts separately.

For future model runs, inspect feature drift with fixed-reference PSI,
Wasserstein distance or KS and track missingness and coverage. Measure
predictive uplift and effect stability by year or quarter, gender, level
and surface. Report group sizes, uncertainty and changes in source coverage
or event composition. Drift by itself is insufficient grounds for feature
removal. Compare raw features with causal cohort-normalized variants using
only prior cohort statistics. No empirical drift results are available.

Future Dataset Builder runs include [D001 and D002](experiments/index.md#planned-dataset-builder-experiments).
