# Parquet row review v1 (Issue #30)

This is a **manual, read-only, incomplete diagnostic**, not a data export or a
training pipeline. Source: `infra/audit/production_parquet_row_review.py`;
privacy boundary: `infra/audit/parquet_row_review.py`.

## Data contract

- The **only source for surface** is PostgreSQL `matches.surface`, matched
  by `canonical_match_id = matches.match_id`. `pbp.surface` is ignored.
- `pbp.point_winner` describes the point, **not the match**. No match winner
  is inferred. `sets_json` emits only the list length and element types,
  not player names, arbitrary JSON values, or assumed score semantics.
- `nbbet_stats.scope=0` is match-wide, `scope>=1` denotes sets per writer.
- No player names, raw IDs, object keys, odds, or raw nested JSON leave the host.
  Match aliases are local to one report, e.g. M1. Unknown strings are dropped.
- Numeric `null` remains distinct from `0`; fractional floats are not
  silently converted to integer fields.

## Bounded sampling

At most 200 lexicographically first listed keys per prefix; up to 3 small
objects (<=256 KiB each) per prefix, <=12 first rows/object, <=24 distinct
match IDs for one bounded PostgreSQL SELECT, <=30 S3 SDK calls, <=8 MiB
accounted GET bytes, <=24,000 bytes output, <=65 seconds internal deadline.
Objects larger than 256 KiB are skipped. Samples are **not representative**,
and this version does **not** guarantee sampling every schema fingerprint.
A successful run means the limited diagnostic succeeded, not that the
whole bucket or every schema was inspected.

PostgreSQL connection is read-only with a 3-second statement timeout and
an explicit rollback. No SQL schema changes, migrations, S3 writes, or
application imports.

## Manual execution

After review, offline tests and explicit authorization to merge and execute
against production, open **Actions → Manual production Parquet row review →
Run workflow → main**. This workflow is restricted to `main` and uses
existing SSH secrets with pinned host fingerprint. No production run has
been performed as part of this PR.

Output is a single JSON line in the GitHub Actions job log. If it reports
`sample_unavailable`, do not infer healthy data. Never post raw server logs
or credentials into issues. The row examples are only for the user's semantic
review; training features and match labels require a separate validated step.

## Offline tests

`python -m pytest -q tests/test_parquet_row_review.py`
`python -m py_compile infra/audit/parquet_row_review.py infra/audit/production_parquet_row_review.py`
