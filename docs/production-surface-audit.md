# Read-only audit of surface markers in tournament names

Proposal only: no production query or workflow has been executed to prepare this
PR. Manual dispatch and merge require separate authorization. matches.surface is
never updated. Existing audits, workflows, collectors, cron and migrations remain
unchanged. This is text-marker coverage, not verification of physical court surface.

## Classification

The standalone script uses Python re with default Unicode-aware word characters,
NFKC normalization and casefold; adjacent combining marks are also rejected as
word continuations. Literal standalone markers only:

| Markers | Class |
| --- | --- |
| хард / hard | hard |
| грунт / clay | clay |
| трава / grass | grass |
| None | unknown |
| Markers belonging to different surfaces | conflict |

Several markers for the same surface are not a conflict. matched_markers contains
unique normalized marker tokens, sorted and separated by semicolons. Substrings
like hardcourt, grassland, суперхард, хардовый and identifiers with underscores
are not matches. Punctuation/hyphens delimit words. No mapping from city, country,
season or tournament identity is used; e.g. Wimbledon without a marker is unknown.
A literal marker can still be semantically misleading (a place called Hard, a
negation, or incorrect source metadata). No API surface enum has been proven.
Do not use this audit as approval for automatic database updates.

## Full coverage, grouping and duplicate handling

Four fixed parameterized SELECTs: session properties/time, schema, totals and
full tournament grouping. There is no SQL LIMIT or sampling in any query.
The required four columns are verified as built-in varchar/date/varchar/uuid on
public.matches before aggregate reads. Only pg_catalog names and explicit public
relations are used with search_path=pg_catalog.

A single grouped query returns exact stored tournament_name values (COLLATE C),
counts and min/max match_date for both all rows and duplicate_of IS NULL, plus
counts of already nonempty surface fields. A non-holdable, forward-only server
cursor fetches batches of 256 until EOF: batching does not limit coverage.
DECLARE/FETCH/CLOSE and BEGIN/ROLLBACK are read-only cursor/transaction control;
the only application query bodies are the four listed SELECTs.
A separate aggregate in the same REPEATABLE READ snapshot checks total matches
and COUNT(DISTINCT tournament_name) against the completed export for each scope.
No partial export is accepted if reconciliation fails.

Names are not trimmed, merged or rewritten before grouping: spelling, case and
whitespace differences remain separate database names. Classification alone uses
normalization. unique_names follows PostgreSQL COUNT(DISTINCT): excludes NULL,
includes empty/whitespace-only stored strings. name_groups_including_null includes
the NULL group, and null_name_matches / blank_name_matches describe missing names.
Both are included as unknown in the match-weighted coverage denominator.
Canonical here means only duplicate_of IS NULL; no assessment of duplicate targets
or discovery of additional duplicates is performed.

## Artifacts and counts

One artifact, retention **1 day**, containing:

- all_tournaments.csv, canonical_tournaments.csv — every name group in each scope;
- all_unknown.csv, canonical_unknown.csv — all unknown groups, not examples;
- all_conflict.csv, canonical_conflict.csv — all conflicting groups;
- summary.json — audit UTC, total matches, unique names, counts and percentages
  for all five categories, automatically classified matches and their percentage,
  missing-name statistics and counts of already populated surface fields.

All six UTF-8 CSVs have exactly:
`tournament_name,matches_count,surface_detected,matched_markers,first_match_date,last_match_date`.
Date bounds ignore SQL NULL dates; both cells are empty if all dates in a group
are NULL. canonical CSV uses date bounds from canonical rows only; groups with
zero canonical rows are absent there. Counts are match-weighted, not name-weighted.
Automatic coverage is (hard+clay+grass)/scope.matches_total. Percentages use Decimal
and round half-up to six decimal places; an empty scope reports 0.0 percent.
Rounded category percentages can differ slightly from 100 in aggregate.

CSV injection protection: names beginning with =,+,-,@ after whitespace/control/
format characters (also NFKC variants) are prefixed with an apostrophe; initial
controls/format characters are protected too. Leading apostrophes are doubled,
leading backslashes receive one extra backslash, NULL is the sentinel `\N`, and
empty string remains an empty cell. This encoding is reversible with original_name
in the script and distinguishes NULL from a literal `\N` name. Standard csv.writer
handles commas, quotes, Unicode and embedded newlines. Do not strip the safety
prefix before opening in spreadsheet software.

The remote script buffers the complete bounded export, then sends a JSON envelope
to stdout redirected to a runner .part file, never the Actions console. No files
are created on production. Runner-only --unpack validates headers, classes,
markers, CSV safety, category subsets and reconciled summary before creating the
fixed artifact filenames. Only a constant completion/error code appears in logs.
No raw database exceptions or connection settings are printed. Artifact access
inherits repository permissions; in a public repository this is not a private
export channel. Titles/counts are intentionally exported, credentials are not.

## Safety limits and fail-closed behavior

- libpq startup default_transaction_read_only=on; driver READ ONLY REPEATABLE READ;
  the first SELECT confirms both modes. No application/health/track_run imports.
- statement_timeout 5s, lock_timeout 1s, connect_timeout 5s,
  idle_in_transaction_session_timeout 10s; watchdog 50s, SSH timeout 60s (+2s kill
  grace), job 4 minutes. Named-cursor FETCH statements have the per-statement
  timeout; the watchdog bounds the entire script. Always rollback/close, no commit.
- Schema/config/dependency failures, query/lock timeouts, unsupported results or
  limits return only fixed error codes and a nonzero exit.
- All CSV documents combined <=8 MiB, JSON envelope <=16 MiB, each raw name <=64 KiB.
  These are resource failure thresholds, not sampling/truncation. If the full
  inventory cannot fit, fail explicitly with export_limit; publish no artifact.
- Runtime uses the existing venv and strict private .env parser for five required
  PostgreSQL fields. No installs, defaults, source/eval, secret dumps or SQL input.
- Existing exact ED25519 fingerprint/StrictHostKeyChecking SSH mechanism, pinned
  checkout/upload actions, contents:read only, manual-only/main-only guard and
  fixed concurrency with no cancellation of an executing audit.

SELECT can still consume CPU/I/O, spill temporary sort files and acquire access-
share locks. PostgreSQL system bookkeeping, SSH audit, interpreter startup hooks
and filesystem atime may cause incidental system writes; no application data,
service state or explicit production files are changed by the audit.

## Tests and separately authorized manual launch

Local unit command (no DB/SSH/network):

```bash
python -B -m unittest discover -s tests -p 'test_production_surface_audit.py' -v
```

Preparation validation also executed all four parameterized SELECTs plus a
non-holdable streaming cursor against disposable local PostgreSQL 16: duplicate
exclusion, distinct counts and per-scope date bounds matched fixtures under
pg_catalog search_path and READ ONLY REPEATABLE READ. The container had no network,
exposed ports or persistent volume and was removed. This does not validate real
production credentials, installed dependencies, schema, plans or load.

Before a future run: review/approve this PR and separately authorize merge;
confirm old deployment remains blocked. After the workflow is present on main,
select Actions → Manual production surface coverage audit → Run workflow → main,
only with explicit dispatch approval. Required existing secrets: SERVER_HOST,
SERVER_USER, SERVER_SSH_KEY, SERVER_HOST_FINGERPRINT. Do not change them here.
Download surface-coverage-RUN_ID, inspect summary.json and all CSVs. Any failure
means the full audit was not delivered: do not infer coverage from partial files
or automatically increase limits/rerun. This PR authorizes no surface backfill.
