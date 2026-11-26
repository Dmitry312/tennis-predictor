> Issue #73: this document describes the optional legacy/manual path. The default
> is [one-dispatch-samples.md](one-dispatch-samples.md), with standing owner policy,
> automatic validation and no per-batch content/rights attestations.

# Original sports record samples — Issue #65

## Policy and evidence

Owner public-sports approval: **2026-11-25**, Issue #65. Real sports names, IDs,
original JSON keys/values, scores, odds and timestamps are retained. No pseudonyms,
score remapping, winner inference or invented records. Review source redistribution
terms and actual staged content before committing. This implementation performs no
production export. Tests are fabricated fixtures, never NB-Bet semantic evidence.

The Issue #62 sanitized API remains separate. Its structural allowlist would remove
keys needed for score mapping. The new tool rejects an entire export on detected
secrets/contact fields instead of editing sports data. Automated checks cannot
prove that free text contains no unrelated personal information; review is required.
Owner approval covers ordinary sports data, not credentials/private material.

## Acquisition

Run scripts/export_match_samples.py on an independently authorized machine using
SELECT-only PostgreSQL and exact-object HEAD/GET S3 credentials. No new production
workflow, SSH session, deployment, upload, automatic Git commit or push is provided.
Dependencies are in pyproject.toml. Use a nonprivileged Linux user.

The tool requires explicit POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER,
POSTGRES_PASSWORD, POSTGRES_DB, S3_ENDPOINT_URL, S3_REGION, S3_BUCKET,
S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY from the environment or --env-file.
There is no default .env search or get_settings() fallback. A bounded literal
.env parser ignores unrelated keys, rejects ambiguous settings and never executes
shell code or expands variables. Do not put credentials in command arguments.
HTTPS endpoint storage.yandexcloud.net and region ru-central1 are enforced.
Dotted buckets use path-style addressing with TLS verification.

PostgreSQL uses repeatable-read, read-only transactions, 5s statement_timeout,
1s lock_timeout and 3s connect_timeout. Sessions failing read-only/timeout checks
are refused. Queries are parameterized SELECT/EXPLAIN against public.matches and
information_schema; no DDL, DML or EXPLAIN ANALYZE. Other public tables with
match_id/canonical_match_id columns are inventoried as **name-based candidates,
not verified foreign keys**, without exporting their rows. Hidden tables or
differently named links are outside this inventory.

Invocation template (replace placeholders, not a real sample):

    timeout --kill-after=2s 130s python scripts/export_match_samples.py \
      --env-file /protected/path/read-only.env \
      --match-id <actual-match-uuid> --match-id <another-actual-match-uuid> \
      --source-manifest /protected/path/exact-source-manifest.json \
      --output /protected/staging/data_catalog

An external timeout is required for a hard deadline during native-library calls;
adjust it if --max-seconds is raised. Built-in alarm, per-request timeouts,
deadline checks, no SDK retries and a 4 GiB address-space limit add bounds.

Optional exact --player or --tournament filters and inclusive
--date-from/--date-to ISO dates (range at most 31 days) cannot be combined with IDs.
Filters return at most --max-matches plus a truncation sentinel. EXPLAIN FORMAT JSON
must show an index/bitmap path, selection-field Index Cond and cost <=10000.
A primary-key scan using only a post-filter is refused. Estimates can be wrong;
statement_timeout supplies the runtime safeguard. No indexes are created. Use exact
UUIDs where current indexes cannot support filters.

## Exact objects and versions

--source-manifest accepts bounded JSON with this shape (replace placeholders):

    {
      "manifest_version": 1,
      "matches": {
        "<actual-match-uuid>": {
          "nbbet_details": [
            {"key":"nbbet_details/<exact-part>.parquet","version_id":"<version>"},
            {"key":"nbbet_details/<other-part>.parquet","etag":"<etag>"}
          ],
          "nbbet_stats": [],
          "pbp": [],
          "nbbet_raw": []
        }
      }
    }

Every object requires explicit version_id, etag or current:true. All listed
versions are retained separately; no implicit newest-row choice or version merge.
HEAD/GET are exact, GET uses HEAD's ETag via If-Match and optional VersionId.
Response size, ETag and requested version are verified. ETag is an opaque
conditional-read token; SHA-256 records actual bytes. 404 becomes object_missing;
permission/other errors abort without SDK exception output. No LIST, writes,
signed URLs, bucket scans or blind history discovery.

--derive-current-parts explicitly opts into current candidates at the four sibling
prefixes of a validated matches.pbp_s3_path. It reuses the repository layout helper
and adds nbbet_raw. Paths are candidates, not proof of existence/history. This
option is mutually exclusive with --source-manifest. Without either, a PG-only
bundle explicitly labels all S3 sources unplanned_or_unavailable.

Rows join only by canonical_match_id and exact UUID. No name matching or guessed
source-ID aliases. A historical file without that column records its schema and
join_column_missing; unrelated rows are not copied. Extending joins requires
source-supported evidence. Ingestion flags and sibling keys prove no completeness.

## Limits

CLI byte flags take integers in bytes, not unit suffixes. All limits are positive
and configurable up to hard ceilings; acquisition/decode/output excess aborts
before publication. Only matching retained rows truncate explicitly.

| Flag | Default | Hard ceiling |
|---|---:|---:|
| --max-matches | 12 | 24 |
| --max-objects | 60 | 100 |
| --max-object-bytes | 8 MiB | 32 MiB |
| --max-total-bytes | 64 MiB | 256 MiB |
| --max-file-rows | 100000 | 500000 |
| --max-total-rows | 300000 | 1000000 |
| --max-source-rows | 5000 | 20000 |
| --max-output-bytes | 32 MiB | 128 MiB |
| --max-uncompressed-bytes | 128 MiB | 256 MiB |
| --max-seconds | 120 | 300 |

source-rows is per match per object/version, not a silent cross-version selector.
matching_rows_observed and rows_truncated make retention explicit. Files are scanned
within decode limits, unmatched rows are not exported. Missing PG, objects, join
columns, raw data, unplanned history and PBP truncation are distinct.
coverage_complete is always false: pbp_completeness=1.0 or ingestion status does not
prove full point history. PostgreSQL JSON is capped server-side at 1 MiB per row (or the smaller output
budget), with an additional UTF-8 byte check; oversize rows abort, never publish
truncated JSON. Additional limits: 128 columns, 128 row groups, bounded
Thrift metadata and batches of 256. Uncompressed bound applies per file/batch.

## Catalog contract and offline reuse

    samples/match_<actual-uuid>/
      matches.json
      nbbet_details.json
      nbbet_stats.json
      pbp.json
      nbbet_raw.json
      manifest.json
    index.json

PG fields/nulls are retained with column types and numeric precision/scale.
Parquet preserves original fields, values and raw_json/players_json strings.
Dates, timestamps, UUIDs and Decimals use the existing exporter codec: strings,
including numeric scale; bytes use binary_base64. This documented JSON
representation does not pretend JSON has PostgreSQL types. Parquet types are
recorded; no decimal-to-float rounding or odds substitution. Both odds_open_p1 and
odds_open_p2 are retained as supplied, including null/invalid prices for later
interpretation. No other price is substituted.

Each object records key, requested/observed version, ETag, SHA-256, last modification,
schema and row counts. Manifest records selection, budgets, metadata inventory,
owner approval, pending content/rights review and incomplete coverage.
DB endpoint/username/DSN and credential configuration are never serialized.
stdout contains counts/flags only; errors are one constant JSON object without
arguments, SQL exceptions, stack traces or SDK diagnostics. Known credentials,
suspicious keys, embedded JSON, connection URLs, private keys and contact fields
are checked before writes; manual review remains necessary.

Deterministic output uses a local exclusive lock and same-filesystem staging.
Files are fsynced; each immutable bundle is renamed atomically and index.json
replaced last as the commit marker. A crash can leave complete unindexed bundles;
reuse ignores them. Rerun identical input to recover. This is not a transaction
across directories. Changed existing bundles are refused: use fresh staging for
another snapshot. Output symlinks are refused; use trusted private directories
with ancestors other users cannot mutate. Concurrent hostile mutation is outside
this local tool's threat model. Ordinary errors clean staging; SIGKILL can leave
unindexed staging files, ignored by Git.

Offline reuse requires no credentials or network:

    python scripts/export_match_samples.py \
      --reuse docs/ml/data_catalog --match-id <actual-match-uuid>

CLI prints a count. Python callers use reuse(root, ids=[...]) for records grouped
by match/source/version after index/manifest/file SHA-256 verification.
Maximum 24 matches and 256 MiB per call; batch IDs for larger catalogs.
Hashes establish consistency, not authenticity. Pending review stays visible;
reuse does not imply rights/content approval. Synthetic entries are never returned
as real samples.

## Review procedure and score mapping priorities

1. Select actual UUIDs with known results: straight sets, deciding tiebreak, BO5,
   retirement, walkover, historical/current details formats. Prefer linked details
   and raw rows; these are acquisition criteria, not invented examples.
2. Separately authorize acquisition and check source redistribution terms.
   Export bounded exact objects to private staging. This PR runs no production
   access. Do not use the older traceback-printing driver.
3. Inspect every actual file for secrets, unrelated personal information,
   omissions, truncation and provenance. Correct source scope on a guard failure;
   do not suppress checks or silently edit sports identities.
4. Compare details sets_json/players_json, raw sports JSON, stats and PBP with the
   known result. Preserve numeric keys; document UNKNOWN semantics rather than
   infer a winner or invent a score mapping.
5. Commit reviewed bundles and index deliberately; record content/rights evidence
   in the PR. Manifest pending is immutable export-time status, not completed
   review. Do not hand-edit checksummed records. Owner approval already covers
   ordinary sports identities; it does not cover secrets or private annotations.
6. Verify offline reuse and tests, then submit through review. No automatic push,
   production dispatch or merge occurs.
