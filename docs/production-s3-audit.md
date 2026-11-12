# Manual S3 metadata audit and PostgreSQL reference reconciliation

Proposal only. Do not merge or dispatch without separate authorization. No
production/S3 connection is made during development. Existing workflows, app,
collectors, cron, Docker, migrations and secrets are unchanged.

## Execution and trust boundary

New `production-s3-audit.yml`: workflow_dispatch only, main-only guard, contents:read
for pinned checkout with no persisted credentials, fixed concurrency (no cancelling
running audits), job timeout 4 minutes. Existing ED25519 discovery is checked
against mandatory SERVER_HOST_FINGERPRINT before authentication; OpenSSH then uses
HostKeyAlgorithms=ssh-ed25519, StrictHostKeyChecking=yes and private known_hosts.
No fingerprint fallback, arbitrary target input or remote script file is allowed.

The checked-out standalone script is streamed into the existing production venv
Python with -I -B. No app/collector/health/track_run import; no installation or
explicit server filesystem writes. No Parquet contents are downloaded. Python
startup hooks, SSH/system logs, PostgreSQL statistics/hint bits and access times
can still produce incidental system writes; this is not a physical immutable
snapshot of the server.

Dependencies boto3, botocore, psycopg2 are checked without installing anything.
Existing `.env` is parsed privately with the same bounded literal subset as the
PostgreSQL audit (64KiB file, 4096-character values, no eval/interpolation/escapes,
no duplicate relevant keys). Matching environment variables take precedence. All
five POSTGRES fields and all five S3 fields must be explicit; there are no guessed
credentials/defaults. Port must be ASCII digits, 1..65535. Configuration is never
printed. Unsupported/missing config fails closed.

Endpoint is restricted to HTTPS storage.yandexcloud.net, region ru-central1, with
TLS verification. Bucket must be a valid non-IP DNS-style bucket name. Explicit
access keys avoid SDK metadata/credential discovery. Alternative endpoints or
URI-style DB references require separate review, not automatic following of URLs.

## Read-only operations and limits

Only ListObjectsV2, HeadObject and optional HeadBucket are permitted. An SDK
before-call guard rejects all other operations (including GetObject, PutObject,
DeleteObject and ACL changes); a wrapper also enforces the allowlist. HeadBucket
is not explicitly called by this script. Existing IAM is not changed: code guards
are not proof that the production credential itself lacks write permissions.
A separately reviewed least-privilege service account would add defence in depth.

Fixed prefixes: nbbet_details/, nbbet_stats/, nbbet_raw/, pbp/.

| Limit | Value |
| --- | --- |
| List page size | 500 objects |
| Pages per prefix | 20 (up to 10000 objects) |
| Total logical SDK calls | 120 |
| Total HTTP attempts, including retries | 160 |
| Retry policy | standard, at most 2 attempts per SDK call |
| SDK connect/read timeout | 2s / 3s |
| Overall cooperative deadline | 90s from script start, including DB |
| Remote hard watchdog / SSH timeout | 110s / 120s (+2s kill grace) |
| Distinct DB paths sampled | 24 |
| JSON output | 16KiB |

A page contributes only after its structure/types/token/object metadata validate.
Repeated/missing continuation tokens fail closed. Only aggregates and fixed local
error codes are printed, never bucket/key/token/URL/credentials or exception text.
Libraries' logging is disabled and remote stderr is suppressed.

## PostgreSQL queries

`SQL` is the exhaustive allowlist of four parameterized SELECTs:
1. Confirm READ ONLY and REPEATABLE READ, obtain database UTC reference time.
2. Verify public.matches.pbp_s3_path is built-in varchar on a table/partitioned table.
3. Count total matches, nonempty path references, distinct nonempty paths, empty
   paths and oversized (>1024 UTF-8 bytes) path references.
4. Select at most 24 unique nonempty paths <=1024 bytes, ordered with COLLATE "C".

No user SQL/shell interpolation. Startup options enforce default_transaction_read_only,
5s statement_timeout, 1s lock_timeout, 10s idle transaction timeout and pg_catalog
search_path; explicit public table names remain resolvable. Connect timeout is 5s.
Driver transaction is READ ONLY REPEATABLE READ; always rollback/close, never commit.
DB transaction is closed **before** any S3 listing/head calls to release its locks.
Counts/distinct/order may scan/sort approximately 199068 matches (owner-reported
context, not a hardcoded count); timeouts bound but do not eliminate read load.

The deterministic sample is lexicographic, not random or representative. Identical
paths are deduplicated both in SQL and defensively in Python; multiple matches
sharing one batch Parquet is normal. Only plain pbp/...parquet keys are checked;
URLs, other prefixes, control characters and oversized paths are not followed.
Invalid sampled paths are counted without disclosure and mark the sample incomplete.
Null/empty PBP references are descriptive counts, **not an error or evidence of
missing applicable PBP**. Match status, retirement/walkover and source coverage
must be assessed before interpreting absent PBP; this audit makes no such claim.

## Report interpretation

- Per-prefix objects_observed, bytes_observed, objects_24h_observed and
  latest_modified_utc summarize successfully validated pages. Each prefix includes
  complete, reason and count_kind: total only for complete=true; lower_bound for
  incomplete scans. Incomplete objects_observed/bytes_observed are observed lower
  bounds, not full inventory totals. No limits were increased. Other prefixes can complete even when one is denied.
- objects_24h_observed means LastModified >= DB reference time minus 24h; it is
  not creation count. Concurrent objects/clock differences can be newer than the
  reference. Latest modification says nothing about Parquet row validity.
- latest_age_hours is (audit time - latest observed LastModified) in hours;
  null for no validated objects. Both timestamps must be timezone-aware datetime,
  normalized to UTC, with years 1970..2100. Arithmetic uses integer microseconds
  before division and checks finite/range output. Future timestamps retain a
  negative age, including subsecond differences. For partial scans this is the age
  of the latest **observed** object, not necessarily the prefix's true latest one.
  Old ages or zero objects_24h_observed do not cause freshness errors: expected
  upload cadence is not established by this audit.
- pbp_head_sample: sample_unique_paths, checked, found, missing, unavailable,
  invalid_paths. A provider 404 is classified missing; 403/timeouts/invalid
  metadata are unavailable, not absence. This does not inspect contents or prove
  that a file actually contains every referring match.
- unavailable_by_reason contains only access_denied, transport_error,
  response_invalid and service_error counters (including zeros). Their sum must
  equal unavailable; checked must equal found + missing + unavailable. A 404 is
  missing, not unavailable. Budget-blocked/unattempted paths do not inflate these
  counters. Other fixed internal failures are folded into service_error, without
  exposing their messages. Prefix reason codes remain fixed and separate.
- Sample complete means all sampled keys got a definitive found/missing outcome.
  all_unique_paths_checked is separate and false when only a subset was checked.
- Top-level complete means all four listings and the **bounded sample** finished
  definitively; it does not imply all database paths were checked or data quality
  is good. Missing>0 may coexist with complete=true: a complete check found issues.
- Incomplete reports exit nonzero and are never presented as full counts. A fatal
  config/dependency/DB/watchdog failure emits only complete=false and a fixed code.

S3 listing is not a multi-page transactional snapshot, and S3 changes can happen
after the PostgreSQL snapshot. A sampled missing object can reflect concurrent
work; investigate without deleting/rebuilding anything automatically. There is
no version/delete-marker inventory or full orphan detection. Do not increase
budgets or run repairs based on this report without separate authorization.

## Validation and future dispatch

Offline tests fake SDK events/responses and DB cursors: pagination, empty bucket,
page/request/time/HTTP budgets, token loops, malformed metadata, duplicate paths,
missing/access-denied/timeout outcomes, URL rejection, write-operation guards,
readonly/schema/SQL bounds, output limits and suppression of secret markers.
Freshness tests include empty/missing LastModified, old/future/subsecond timestamps,
invalid types/ranges/timezones, partial lower bounds and error-counter invariants.
No real S3 SDK/service, credential permissions or production data is tested.
All four SQL queries were additionally checked via PREPARE/EXECUTE on a disposable
local PostgreSQL16 with pg_catalog search_path, READ ONLY REPEATABLE READ, and
fixture duplicates/null/empty paths. The network-isolated, volume-free test
container was removed; no production database was contacted.
YAML/Bash/Python/actionlint and git diff --check are also required before review.

After separately authorized merge, confirm deployment remains blocked, review
main SHA, dependencies/config and query/list budgets, then request explicit
permission to dispatch. No artifacts containing raw responses/paths should be
uploaded. Do not rerun incomplete audits automatically or install missing packages.
