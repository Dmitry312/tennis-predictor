# Full match examples and full-history semantic verification

## Objective
Select 10–20 *same* canonical match IDs across PostgreSQL `matches` and
S3 `pbp/`, `nbbet_stats/`, `nbbet_details/`. Preserve all original
sporting-data column names, JSON keys and values. Do not anonymize players.

## Sample extraction (next implementation stage)
1. Inspect PostgreSQL table definitions, foreign keys and S3 object partition
   layout before coding a join. The previous audit's first 200 keys per prefix
   were **not** representative and did not establish cross-source joins.
2. Pick deterministic, documented match IDs with coverage combinations,
   two/three/five sets, tie-breaks, retirements and walkovers where available.
3. Export the **complete matching records**, not just the first row of a file.
   Include source path, extraction timestamp, PostgreSQL schema/table, data
   types, row counts, nulls and nested JSON without renaming.
4. Exclude connection credentials, environment files, database role metadata
   and infrastructure configuration. Sporting data and player names may stay.
5. Use a size-capped GitHub artifact with short retention, rather than dumping
   raw JSON into public workflow logs. Fail if output is unexpectedly large.
6. Keep production access SELECT/read-only, time-limited and bounded; separate
   sample export from the later full-history validation.

## Data dictionary
For every field: source, table/path, actual key, data type, nullability,
example, semantic interpretation, player orientation, evidence,
confidence, missingness, and whether usable pre-match (leakage risk).
Explicitly distinguish verified facts from hypotheses.

## Full-history validation (after sample semantics are confirmed)
- Enumerate all eligible historical match IDs with stable pagination and
  checkpointed progress; don't assume an S3 object equals one match.
- Independently compare recovered winner and set score against a trusted
  reference, accounting for retirements, walkovers, unfinished matches and
  tie-break representations.
- Report totals, coverage, errors by year/surface/tournament level/status,
  schema fingerprints, missing joins and bounded mismatch examples.
- Gate promotion of labels on independently verified agreement and documented
  treatment of unknowns. Never silently convert null/unknown into a loss.

## Current implementation status
`infra/audit/match_score_validation.py` supplies an offline comparison
primitive with tests. It does **not** perform S3/PG extraction or validate
the full history. The production exporter requires a separate reviewed PR and
explicit approval before execution.
