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
