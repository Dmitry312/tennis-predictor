# Parquet Schema Audit v1 — execution contract

Status: **design for review, not yet executable**. This document does not authorize production access or deployment.

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
