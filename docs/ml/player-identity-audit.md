# Bounded player identity audit — Issue #52

Implementation extends PR #51 without replacing its original offline identity_audit.py.
No production execution is authorized by this document.

## Sources and sampling
foundation.md and details.py establish that players_json preserves match["3"]/["5"];
field "1" is a name, "2" a photo, "3" a flag, and "4" ranking data. None is promoted
to a stable ID. detail_job.py:flush writes both nbbet_details and nbbet_raw using
gender={M,F}/level={tour,challenger}/year={match_date.year}/part-...parquet.
The raw payload is the entire pageTennisEvent, not just match.

For each source/gender/level, one delimiter listing discovers up to 128 year prefixes.
Select earliest/middle/latest observed years; inspect one 128-key page per selected
year and the lexical middle eligible small Parquet object. ITF is excluded.
No pagination or search outside these fixed partitions. Missing strata, truncated
listings and skipped large/non-Parquet objects are explicit aggregate counts.
Selection is biased toward small files and observed lexical ranges, not representative
of all years, players, tournaments or matches. Empty samples do not establish absence.

## Evidence
Only the selected JSON column is decoded. Name association uses the documented
field "1" for match player objects and a clearly labelled heuristic for other objects.
Outside-match traversal includes nested arbitrary-key mappings; potential mapping keys
are represented by a synthetic field_64 in a separate mapping_key scope. Never emit
original dynamic keys. This synthetic path is not a source schema key.
Unknown field names become wildcards; fixed numeric schema keys 0..64 become field_N.
Wildcards can conflate fields: no stable ID can be declared from these counts alone.

Per path/type: observation counts, objects-with-path / player-object denominator,
distinct/repeated values, cross-year repetition, cross-year single-name values,
one-value/multiple-name and one-name/multiple-value counts, cross-gender collisions.
Relations are separate per source/scope/path/type; there is no unverified cross-source join.
Values/names are hashed only in memory; neither hashes nor values are exported.
Known name/photo/rank fields are explicitly marked non-ID. Other candidates remain
unverified, even if unique or repeated. Never merge ambiguous names automatically.
Rows are conservatively deduplicated by source/year/gender/JSON hash, not match ID:
identical player payloads from different matches may collapse; corrections can remain
separate. Counts are not independent-player estimates. Cross-year absence is unknown
when repeat observations are missing. Orientation/result semantics are not audited.

## Hard limits and failure
32 LIST calls, 24 GET calls, 128 entries/page, no retries (one SDK HTTP attempt).
24 files <=256 KiB, <=256 rows/file (6144 total), <=64 columns, <=16 row groups,
<=4 MiB declared uncompressed data/file and decoded selected-column bytes.
65-second cooperative deadline, 95-second process alarm, 110-second remote timeout;
4 GiB address-space limit and one BLAS/OMP thread. SDK connect/read timeouts 2/3 seconds.
JSON <=64 KiB/row, depth/node/container/path budgets; output <=32 KiB.
LIST calls reserve 1 MiB of headroom before I/O; parsed listing size is charged conservatively.
GET sizes reserved before reading, ETag/IfMatch and exact length/status validated;
streams close on errors. LIST accounting uses a conservative serialized-size estimate.
LIST parsing trusts the configured S3 service to honor MaxKeys/key bounds; it is not
a hostile-server HTTP parser. Native Parquet decompression is additionally bounded
by address space; malformed metadata cannot authorize unbounded decoding.
Only ListObjectsV2/GetObject are allowed; HTTPS destination checked on every attempt.
No fallback to broader scans, DB, retries, arbitrary endpoints, writes or source dumps.
Errors emit only identity_audit_unavailable and fail nonzero; hard kills can emit no JSON.
A finished bounded scan still reports complete=false and population_coverage=unknown.

## Manual launch — only after explicit approval
1. Review the exact ML commit, script, tests and permissions. Do not dispatch this PR.
2. Configure protected Environment player-identity-audit: required reviewer, no bypass
   where supported, deployment branch ML only. Reviewers must verify reviewed_sha.
3. Existing secrets: SERVER_HOST, SERVER_USER, SERVER_SSH_KEY, SERVER_HOST_FINGERPRINT.
   Existing production path/venv and literal .env are reused. No sudo or server files.
   Existing account/IAM may be privileged: prefer separately scoped credentials; guards
   do not prove IAM read-only. Nothing here expands permissions or installs packages.
4. GitHub workflow_dispatch requires the workflow file to exist on the repository's
   default branch. Merely merging into ML is insufficient if default remains main.
   A separately reviewed publication of the workflow on the default branch is needed;
   do not change the default branch or merge automatically to work around this.
5. Once available on default and reviewed code is on ML, obtain its exact 40-character
   SHA and (only with approval) run:
   gh workflow run production-player-identity-audit.yml --repo Dmitry312/tennis-predictor --ref ML -f reviewed_sha=REVIEWED_40_CHARACTER_SHA
   The job rejects other branches and mismatched selected/reviewed SHA.
6. Review aggregate output for unknowns before any subsequent identity design. No
   artifact upload or raw payload review is authorized.

## Offline verification
CI ml-identity-audit-tests.yml runs existing synthetic tests plus
tests/test_player_identity_s3_audit.py with fake S3 and in-memory Parquet. It validates
stratification, aliases/collisions, redaction, mapping keys, malformed/oversized inputs,
budgets, operation allowlist, empty/truncated listings, stream closure, and workflow
YAML/Bash safety. It has no production secrets or production audit steps.

## Failure diagnosis — Issue #54

Run 45654718056 / job 139353029391 used reviewed ML SHA
15704039764531fe4c1a61beecd4abe986c829f8. SSH/fingerprint passed; the process
returned identity_audit_unavailable after about three seconds. This establishes
remote Python execution, not successful configuration, S3 access or decoding.
The old catch-all discarded the failure stage; its root cause cannot be recovered
from that message alone. No production execution was performed for this fix.

Failures now emit only complete=false, bounded_scan_finished=false, the existing
error label, and fixed allowlisted stage/reason codes. Stages: configuration,
dependency, resource, list, get, decode, evidence, output. Reasons distinguish
validation, missing settings, unavailable schema/dependencies, timeout, resource
exhaustion, access denial, missing objects, changed objects, service/transport
errors, budget exhaustion, output limit and internal error. Exceptions, SDK
messages/codes, config values, URLs, paths, object keys and payloads are never copied
into the diagnostic. Nested stages retain the innermost classification.
No partial evidence is emitted after a failed scan: a failure can occur while
updating counters, so returning that state could mislead users.

Checks against the existing working production_parquet_row_review.py:
- Same literal endpoint and region, five S3 settings, existing .env location and
  explicit credentials; identity audit still does not require PostgreSQL settings.
- Found a real compatibility discrepancy: the identity bucket regex excluded dots,
  while the working review accepted dotted DNS bucket names. Dotted names now work;
  adjacent dots and IPv4 literals remain rejected. This discrepancy is not evidence
  that the production bucket is dotted or that it caused the observed failure.
- The writer detail_job.py:flush uses gender/level/year partitions for details/raw.
  No wider listing, fallback path, pagination, retries or privilege changes were added.
- Realistic botocore Stubber responses exercise ResponseMetadata, HTTP headers,
  timezone-aware LastModified, quoted ETags, StreamingBody, conditional GET and actual
  operation-guard registration, entirely offline. SDK error responses are classified
  by fixed HTTP status mappings, never their arbitrary message or service code.
- Resource limits and dependency imports are separate stages. The existing 4 GiB
  address-space cap can fail on a constrained process/runtime; it is not raised or
  disabled. Missing players_json/raw_json is now decode/unsupported_schema rather
  than an opaque generic error. Neither case is confirmed as the production cause.

Interpretation: configuration/missing_setting means explicit literal settings are
incomplete; configuration/access_denied or not_found can describe reading the config.
dependency/dependency_unavailable describes import failures; resource failures do
not authorize weakening the limit. list/get access_denied describes SDK access,
object_changed is an HTTP 412 conditional-read failure. decode/unsupported_schema
requires offline schema compatibility review. Other validation/internal errors
remain deliberately coarse. A timeout or native hard kill can yield no JSON.

All prior operation, attempt, byte, row, JSON, memory, output and deadline limits
remain unchanged. Production workflow/publication and its reviewed-ref/fingerprint
requirements are unchanged. A future separately approved run can identify the stage;
this PR neither dispatches it nor promises that one particular cause has been fixed.
