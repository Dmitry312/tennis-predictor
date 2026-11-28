# Historical rating and model baselines

[Issue #107](https://github.com/Dmitry312/tennis-predictor/issues/107)
implements the working cohort from the [statistics decision](stats-coverage-decision-2026-11-27.md),
[frozen quality register](data-quality-register-2026-11-27.md) and
[surface repair](surface-repair-2026-11-28.md). The three policy records were
ported from main at `2639cc2c818576ebb75f9670173fcbabd3312242`. The
107,895 audit denominator and projected 106,968 joint-readiness proxy count
are unrelated to the model’s measured target count.

## Source and cohort

Pinned PostgreSQL match rows are authoritative for metadata, including the
repaired surface. The read-only exporter uses one repeatable-read cursor
with bounded pages, statement and overall deadlines. It downloads CURRENT
of the published SQLite registry with manifest, token and SHA verification.
For result evidence it lists only NBBet details partitions for 2021 onward,
then conditionally reads each inventory object once and decodes only
match ID, NBBet ID, status, set score and fetch timestamp. It uses neither
raw PBP, stats, source refetches, S3 writes nor per-match remote requests.
The frozen quality register provides aggregate checkpoints; a reusable raw
result bundle can bypass extraction entirely. Fresh extraction uses a bounded
details inventory because the published audit aggregates cannot reconstruct
individual legal winners. PostgreSQL transaction ID and time, details
inventory digest, projected file hashes and registry token bind the saved
bundle. This is a frozen research export, not a cross-store transaction.
S3 result rows received after the PostgreSQL snapshot are excluded.

The initial history starts in 2021 with Tour/Challenger singles for both
genders including qualifiers. The filter excludes ITF, Davis Cup and
BJK/Fed Cup aliases, Asian Games, exhibitions and identified doubles.
Unknown tournament names are excluded. Use the same canonical score parser
and NBBet side-link contract. For the bounded export, use the newest details
receipt no later than export cutoff, rejecting competing scores at that
receipt, mismatches, non-completed status, duplicates, illegal scores and
missing or equal approved identities. Status 3 and a legal winner suffice
without extended statistics. Known hard, clay, grass or carpet is required
for a target. An otherwise valid unknown-surface result still feeds global
ratings and creates no surface state. Both players must have exactly one
record on the UTC date for target inclusion. Eligible same-day facts remain
in daily frozen batch updates. Reports give disjoint exclusion counts and
segment detail. Source tournament aliases and activity differ over time;
the waterfall is conditional on the evidence in the captured bundle.

## Registered comparison

2021 warmup, 2022–2023 initial training, rolling-origin validation of 2024
and 2025, untouched 2026 holdout ending before the source’s partial UTC day.
These are experimental boundaries. The 2025 fold can train on 2024. Ratings
see only earlier UTC days, and historical replay assumes earlier valid
results are available retrospectively. This assumption does not establish
strict-as-of or live readiness. Compare default 2021 warmup with a bounded
2022 alternative on validation. Pre-2021 warmup and recovery are omitted.

E00 measures default Global Elo. E01 adds the five Rating Engine models and
its registered one-factor perturbations. E02 is standardized Logistic
Regression, E03 is CPU CatBoost with fixed shallow parameters. Supervised
learners use the default pre-match ratings, differences, probabilities,
counts, RD, volatility, days since rated activity, newcomer flags and
gender/level/surface. Player IDs, target residuals, official rank and odds
are excluded. Both orientations enter training. At inference, average
the direct score with one minus the reversed score. Scaling is fit on past
training data per fold. No extra calibration transform is fit. Log loss is
primary. Brier, ROC AUC, fractional-tie accuracy and fixed calibration bins
are reported overall and by year, gender, level and surface. AUC is undefined
for one class. Validation compares the same targets and freezes rating
choices before any holdout command.

## Running

The **Historical baselines read-only evaluation** workflow defaults to
validation and needs no manual parameters. Its fresh-read job uses the
existing `player-identity-audit` environment and may require GitHub reviewer
approval. It uploads aggregate metrics only unless the environment already
has a high-entropy `BASELINE_SNAPSHOT_PASSPHRASE` secret configured to retain
the source as a short-lived encrypted artifact. To open holdout later, use
explicit `holdout` plus the validation run ID. This reuses the encrypted
frozen input and frozen validation report; no fresh source reads are needed.
If retained source is unavailable, run locally with an owner-provided
frozen bundle. Keep source, rating SQLite files and artifacts outside Git.

```bash
uv run --locked --group baseline python scripts/run_historical_baselines.py \
  --source /private/frozen-source > /private/validation.json
uv run --locked --group baseline python scripts/run_historical_baselines.py \
  --source /private/frozen-source --held-out /private/validation.json > /private/holdout.json
```

### Real catalog smoke

`--catalog docs/ml/data_catalog` uses the real 20-match catalog and the
local test registry from PR #101. For this smoke only, supervised training
starts in 2021, giving two past examples in the 2024 fold. The production
experiment still reserves 2021 for warmup. Exactly one validation target
(NBBet 531103) was eligible and none in 2025. Three pre-2026 results qualified
as rating facts. All models, including fitted Logistic Regression and
CatBoost, produced 0.5 for the common target. Log loss was 0.693147, Brier
0.25, accuracy with fractional ties 0.5, and AUC undefined. The smoke
cannot rank the models. The registered holdout was not opened.
