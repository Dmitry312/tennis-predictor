# Parquet Schema Audit v1 — execution contract

Status: **implemented for offline review; production execution not authorized**.
This document does not authorize production access or deployment.

## Objective
Discover actual Parquet schema, nested field types, row-group and row counts, and structural consistency across `pbp/`, `nbbet_stats/`, `nbbet_details/`. Do not assume each prefix contains only Parquet; classify extensions first. Do not assert winner/score semantics based on column names alone.

## Data handling and security
- Standalone manual `workflow_dispatch` on `main` only, pinned checkout, trusted ED25519 host fingerprint, no persisted GitHub credentials, SSH command streams script to existing production venv.
- No application imports, no collector invocation, no migrations, no database or S3 writes.
- Restrict S3 to HTTPS `storage.yandexcloud.net`, region `ru-central1`, and explicit configured bucket/credentials, with TLS verification.
- Allow only `ListObjectsV2`, `HeadObject`, and **bounded** `GetObject` byte-range reads needed for Parquet footer metadata. Deny full-body reads, uploads, deletes, and any other SDK operations via allowlist guard. Existing IAM may still permit writes: guards are defense-in-depth, not proof of read-only credentials.
- Never log object keys, URLs, player names, match identifiers, credentials, S3 responses, exception strings, or sample row values.
- No raw match data or artifacts uploaded to GitHub. Output only fixed-field aggregate JSON and schema field/type names. Field names may themselves contain sensitive content: validate printable ASCII and cap lengths, or report redacted counts.
- Keep schema introspection separate from content sampling. No `read_table` or `read_row_group` in v1.
- Parse the existing production `.env` with a bounded literal-only parser, never execute or interpolate it; reject missing/ambiguous config.

## Fixed budgets (v1)
- Prefixes: `pbp/`, `nbbet_stats/`, `nbbet_details/`; `nbbet_raw/` is out of scope.
- Up to 2 listing pages of 500 keys per prefix, maximum 6 sampled Parquet objects per prefix, selected across lexicographically separated positions within observed keys (sampling not statistically representative).
- Up to 18 objects; at most 2 ranged GETs per object (last 8 bytes and footer); maximum footer 1 MiB, maximum cumulative response body 12 MiB.
- Reject malformed/too-large footer lengths before requesting footer. Validate Parquet magic, length, HTTP ContentRange, response length and `ContentLength` against `HeadObject` size.
- 75-second cooperative deadline, 110-second hard timeout; 2-second connect / 3-second read timeout, max 2 SDK attempts; bounded total SDK/HTTP attempts.
- Per-object errors classified as `not_parquet`, `invalid_footer`, `unsupported_schema`, `access_denied`, `transport_error`, `service_error`, `budget_exhausted`; no raw errors.
- Max JSON output 32 KiB. Every incomplete prefix must say `complete=false`, with observed counts explicitly labelled lower bounds.

## Report
- Counts of observed keys and extensions, and sampled Parquet objects by prefix.
- Per sampled file: **no object path**; file size bucket, row groups, rows, schema fingerprint, column paths and logical/physical types, optional compression and nullability metadata.
- Per prefix: schema fingerprint frequencies, total sampled rows, errors and completeness flags.
- Highlight candidate fields for match ID, score, winner, service/return statistics and point events, but mark semantic verification `pending` until a separately authorized bounded content inspection.
- Do not infer match counts from row counts: PBP row grain may be points, matches, or batch records.
- Compare the discovered schema with the application writer implementation and record any ambiguity.

## Acceptance and rollout
1. Add standalone implementation and offline tests using fake boto3 range responses and synthetic Parquet fixtures. Include malformed footer, oversized metadata, range ignored (HTTP 200), pagination, timeout, redaction, output cap, operation guard, and zero-Parquet-prefix cases.
2. Validate Python compilation, pytest, YAML, shell and workflow safety in CI.
3. Review Draft PR, resource bounds and security before merge.
4. Production execution requires **separate explicit approval** after merge. Do not dispatch automatically.
5. Use the resulting aggregate report to design a second, content-level sample if needed for target labels and feature provenance.

## ML requirements confirmed
- Train only on Tour and Challenger; exclude ITF.
- Odds are **not features**. For historical betting validation, use stored `odds_open_p1/p2` as provided, regardless of backfill timestamp.
- Result, score, match statistics and PBP are sourced from S3; do not invent a winner column in PostgreSQL.

## Implemented interface (Issue #28 / draft PR #27)

The standalone `infra/audit/production_parquet_schema_audit.py` implements this contract.
The production workflow remains manual-only on `main`; adding or updating this PR does
**not** authorize dispatch. It streams the reviewed script into the existing production
venv with `-I -B`, without application imports or persistent server files. It follows
existing audit deployment paths and secrets (`SERVER_HOST`, `SERVER_USER`,
`SERVER_SSH_KEY`, `SERVER_HOST_FINGERPRINT`), including the existing privileged SSH
account if configured. No new privilege escalation is performed; a dedicated restricted
account and read-only S3 credentials remain recommended before an approved rollout.

The literal parser reads only the five required S3 settings from the existing `.env`.
It rejects duplicate, missing, interpolated or unsupported values rather than applying
application defaults. HTTPS endpoint, region and bucket are validated. No database
connection or odds classification occurs in this audit.

### Reading and interpretation

- Only ListObjectsV2, HeadObject and ranged GetObject are allowed, with a second guard
  on botocore operation parameters and a destination check before every HTTP attempt.
- Maximum 60 SDK calls / 120 HTTP attempts (two attempts per call), additionally capped
  globally at 156 HTTP attempts. No pagination beyond two pages per prefix.
- GET byte counts are reserved before I/O, including failed/partial reads. Parsed LIST
  response bytes are also charged. The 12 MiB budget is shared, so large listing responses
  can reduce the number of footers inspected. LIST response size relies on the trusted
  S3 service honoring MaxKeys and key-size limits; the SDK parses these responses before
  the audit can account for them. This is not a hardened reader for an arbitrary HTTP server.
- HEAD size and ETag are checked against both GET ranges; IfMatch prevents mixing
  versions. Require HTTP 206, exact ContentRange/ContentLength and matching ETag.
  Ignored ranges are closed without reading their body. Streaming bodies always close.
- The last eight bytes provide footer length and trailing PAR1 magic. The second range
  reads only the advertised footer. The original leading magic is **not** fetched: that
  would require a third range and violate the contract. Arrow receives a synthetic PAR1
  envelope, never original row bytes. This verifies footer metadata, not entire-file
  integrity, row-page readability, or the truthfulness of a malicious footer length.
- Arrow Thrift string/container limits constrain metadata parsing. All custom metadata
  and unknown field paths are suppressed. Only writer-known field names and fixed type
  categories are reported; arbitrary printable ASCII names are also redacted.
- Fingerprints identify the Arrow schema without top-level custom metadata; field
  metadata and nested structure may affect the hash. Hash differences alone do not
  establish incompatible model inputs. Logical type parameters are not fully rendered.
- `complete` means the bounded listing and selected metadata reads finished; it never
  means all files were inspected or all PBP is complete. Every incomplete prefix marks
  observed counts as lower bounds. A zero-Parquet prefix reports zero samples, not a
  transport error. Non-Parquet and directory keys are counted without reading them.
- Output exceeding 32 KiB is replaced with a small incomplete report and a nonzero exit.
  A hard timeout may terminate before any JSON is produced; it must never be interpreted
  as a healthy audit. No artifacts are uploaded.

### Application writer comparison and semantic limits

`ingestion/nbbet/pbp_frames.py:PARQUET_COLUMNS/build_parquet_frame` defines point-shaped
PBP fields, including reconstructed `point_winner`, `server`, score fields and
`game_complete`. `ingestion/nbbet/details.py:STAT_FIELDS/details_to_rows` defines
per-scope service/return statistics and the details record (`sets_json`, `status`,
`players_json`, ranks and coverage). The report exposes only these known field names
plus their physical/logical categories and nesting levels. Candidate identity fields
are `canonical_match_id` / `nbbet_match_id`; candidate point/score fields are
`point_winner`, `server`, `score_server`, `score_returner`, `set_num`, `game_num`,
`point_num`, `sets_json`; candidate service/return fields follow `STAT_FIELDS`.
All semantic verification remains **pending**. `sets_json` is a string schema: footer
inspection cannot reveal its JSON structure or establish a match winner.

`pbp_completeness=1` is not validated here. Sample row totals are not match totals.
No content values, object keys, player names or match IDs leave the server. Schema
sampling does not authorize training on ITF: ML remains Tour/Challenger only. Odds
remain excluded from features; stored odds remain authoritative for historical
betting validation regardless of capture/backfill time.

### Offline validation and rollout

Run:

```sh
python -m pytest -q tests/test_production_parquet_schema_audit.py tests/test_parquet_schema_audit_workflow.py
python -m py_compile infra/audit/production_parquet_schema_audit.py
```

Fixtures are synthetic in-memory Parquet and fake S3 responses; they need no credentials.
CI runs these tests, YAML/manual-trigger/checkout checks and Bash syntax validation.
Tests cover malformed/oversized metadata, ignored/invalid ranges, pagination, deadlines,
byte/request/attempt/output budgets, ETag changes, operation guards, redaction, nested
schema and empty/non-Parquet prefixes. Existing PostgreSQL integration suites require
a disposable local database and are unrelated to this S3-only audit.

Keep PR #27 draft pending security review. Merge and production dispatch require
separate authorization; neither is performed by implementation or test CI.
