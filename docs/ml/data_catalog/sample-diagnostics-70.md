# Sample staging diagnosis — Issue #70

## Evidence from run 45767421900

The failed run selected ML bbdd705ebdeeb12d312848a4df6831212ecd8478, matching
reviewed_sha; stage/offset 0/size 2/root opt-in were present. Checkout and CPython
3.11.18 setup succeeded. The shell step lasted about four seconds and emitted only
sample_ssh_failed. stderr was discarded for request construction and SSH, while
pipefail collapsed SSH and capture failures into one code. These logs cannot prove
which remote stage was reached or whether authentication succeeded.

The Node 20/24 action warning occurs after successful checkout/setup and is not
evidence of the cause. Existing action SHAs are kept. Runner imports were inspected:
request/build/capture helpers and their transitive module-level imports are stdlib;
boto3/psycopg2/pyarrow imports occur only in remote execution/Parquet reads. New clean
Python 3.11 tests run BEFORE pip installation, including the historical runner source
and full fake-SSH path. No speculative project dependency install is added to production.

A demonstrable SQL defect was found: SELECT match_id::text ... ORDER BY match_id
resolves ORDER BY to the text output alias, not the UUID input column. PostgreSQL 16
plans a sort, which the bounded candidate-plan guard rejects. The regression creates
an isolated synthetic PostgreSQL 16 database, reproduces that rejection and verifies
ORDER BY m.match_id from public.matches m uses the UUID primary-key path. The fix
retains the same LIMIT, keyset cursor, pool budget and guard; no index/migration,
Seq Scan permission or broader search was added.

This reproducible defect is a real failure path, not proof of the historical run's
exact cause. Because the old run suppressed all relevant stderr, its authentication,
runtime, config, database plan and source availability remain unproven. No production
connection or rerun was made to fill those gaps.

## Fixed diagnostic protocol

stderr accepts only sample_diag:<stage>:<status>:<reason>, with fixed allowlists.
No exception messages, tracebacks, SQL text, DSNs, credentials, paths, source keys,
rows, match IDs, fingerprints or arbitrary stdout/stderr are reflected. Public
stdout remains only the existing whitelisted staging receipt/counts or redirected
private transport bytes. Detailed source material never becomes an Actions artifact.

Stages cover runner tools/Python, request, key discovery/verification, transport,
remote entry/runtime/bootstrap, per-package dependency versions, configuration,
PostgreSQL connect/session/selection/rows/close, exact manifest, S3 client/HEAD/GET,
Parquet decoding, content validation, local catalog write/archive/private staging,
release/cleanup and capture. Start/ok/error markers distinguish last successful stage.
Original exceptions retain type for callers; a marker avoids replacing a previously
reported inner failure with an outer generic failure.

Selected reasons include missing dependencies/config, incompatible versions,
schema/permission errors, readonly/statement-timeout guard failures, SQL timeout,
rejected/empty selection, conditional object changes, byte/decode limits, sensitive
content, private-stage capacity/expiry/tampering and capture/digest errors.
SQLSTATE and S3 status are mapped only against fixed values. Remote stdout is never
a diagnostic channel; malformed content fails capture without reflecting bytes.

Bootstrap emits fixed reasons even before the packaged modules can load. The
remote shell emits an entry marker only after SSH authentication and fixed reasons
for missing project/interpreter/timeout. Host verification never exposes actual keys.
Known OpenSSH auth/DNS/connect/key errors map to fixed categories; otherwise the
transport reports auth_or_transport_failed without inventing a diagnosis.

The relay bounds total stderr at 64 KiB and individual lines at 512 bytes, accepts
only exact ASCII records and drops unknown text. Both PIPESTATUS entries are saved
immediately. The relay is explicitly awaited, so a safe remote error survives a
nonzero SSH pipeline. Capture errors are secondary/skipped on upstream failure.
An opaque success summary is committed only after SSH AND capture succeed.
Temporary request, key, FIFO, diagnostic files and private summaries are cleaned.

## Offline verification and remaining operational checks

The clean-runner job uses no pip/project installation and no real SSH endpoint.
Fake binaries cover valid/mismatched host keys, auth rejection, malformed capture,
bad inputs, remote failure preservation and an actual streamed bootstrap into a
dependency-free local fake server. Tests assert no secret/row/path leakage and
temporary-key cleanup. The separate PostgreSQL service is an ephemeral CI fixture
only; production settings/secrets are not used.

Existing source/read limits and settings are unchanged: at most 512 candidate rows
plus sentinels, initial batch 2/max 5, exact S3 objects/no LIST, per-object/total byte
and decode limits, read-only repeatable-read DB with 5s SQL timeout, outer deadlines,
pinned ED25519, reviewed SHA and existing account opt-in. No source permission or
publication scope was expanded.

Read-only Environment inspection through the connected GitHub fetch endpoint was
rejected as an unsupported endpoint. No connector environment/deployment API is
available. Therefore match-sample-review required-reviewer, self-review and ML-branch
restrictions could not be confirmed. A quick run or absent visible approval wait
does NOT prove those protections are configured or absent. An authorized admin must
verify them using existing GitHub administration access; this PR changes no protection.

Actual SSH reachability/authentication, the server's Python/virtualenv/package ABI,
complete explicit .env, PostgreSQL schema/plans and S3 permissions remain unverified.
The new checks identify them on the next separately authorized run. Missing or
container-only runtime is an operational blocker, not a reason to install packages,
change production paths/permissions, bypass plans or grant root access automatically.

This implementation performs no production dispatch, merge or real-data publication.
