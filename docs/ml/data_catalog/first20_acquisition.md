> Issue #73: this document describes the optional legacy/manual path. The default
> is [one-dispatch-samples.md](one-dispatch-samples.md), with standing owner policy,
> automatic validation and no per-batch content/rights attestations.

# First-20 samples: private staging, review, automatic PR — Issue #67

The default process no longer needs a new SSH user, certificate, manual encryption,
archive download or manual catalog import. It reuses existing SERVER_* secrets,
pinned SSH, deployed Python and the existing exporter. This PR prepares the
workflow only: no production dispatch, connection, actual sample publication or
merge has been performed.

Owner approval dated 2026-11-25 permits public ordinary sports names, IDs, original
keys/values, scores, odds and timestamps. It does not cover credentials, unrelated
personal information or source redistribution restrictions. Creating a sample
branch already publishes data. Review therefore happens BEFORE any GitHub content
upload or sample-branch creation, not after opening a public PR.

## Default operator steps

1. Run production-match-sample-acquisition.yml on ML with operation=stage and the
   exact reviewed ML commit SHA. Default batch size is 2, maximum 5. The same
   existing protected match-sample-review Environment approves execution.
   The job has contents:read only and no publication token. It stages a bounded
   batch privately on the existing server. Public output contains only an opaque
   ticket and archive SHA-256, never source rows, source errors or credentials.
2. Using existing authorized SSH access, inspect the actual JSON at
   /tmp/tennis-match-review-<UID>/<ticket>/review/. This directory and its parent
   are mode 0700; the transport ZIP/metadata are 0600 and owned by the SSH account.
   Inspect every original record, omissions, source schemas and provenance; verify
   source redistribution rights for these actual records. Keep references to the
   content/rights review. The ticket's stage.json identifies the exact archive
   SHA-256. A hash or generic Environment approval alone is NOT content inspection.
   The reviewer needs already-authorized access to these private files; do not
   print them through Actions, publish them as an artifact or change permissions.
3. Run publish-reviewed-match-samples.yml on ML with ticket, exact inspected digest,
   content_reviewed=true, rights_reviewed=true and two nonempty review references.
   Supply references only, not source content or secrets: dispatch inputs themselves
   are visible to repository readers. The reviewed SHA must match the stage's source
   SHA and selected ML SHA. Protected Environment approval must verify these facts.
   Release refuses missing reviews, wrong/changed/expired bytes or wrong source SHA.
4. The isolated publication job fetches ONLY that reviewed stage over pinned SSH.
   It does not read .env, connect to PostgreSQL/S3 or call acquisition. No GitHub token
   is sent to the server. It verifies the ZIP, exact regular-file paths, checksums,
   secret/contact guards, plan, batch count and review digest again. It merges the
   catalog index without dropping synthetic entries or changing existing snapshots,
   then creates samples/first20-<ticket> and a PR into ML automatically. Only this
   job has contents:write/pull-requests:write. Its GitHub token is injected only into
   the final publisher step; SSH credentials exist only in the preceding fetch step,
   whose temporary key is deleted on exit. There is no automatic merge.
5. Check the generated PR, including original JSON, selection plan and review receipt.
   Source samples, plan and receipt are ordinary reviewed Git files, not artifacts.
   After PR creation the workflow automatically removes the server stage. If that
   cleanup fails, use operation=cleanup with the ticket in the staging workflow.
   Cleanup never touches production services, data tables, S3 or unrelated directories.
   Runner ZIP/review files are deleted on success/failure. Failed publication leaves
   the private stage for diagnosis/retry; it does not quietly publish unreviewed data.

Staging and publication are separate workflows (9 and 8 inputs, within GitHub's
10-input limit), with separate read/write jobs. No automatic source access or sample publication occurs when this implementation PR
is updated. A human can attest a completed review; software cannot prove that a person
read the data or that a legal interpretation is correct. The protected Environment
and truthful digest-bound attestations are mandatory operational controls.

## Existing runtime and permissions

Reuse SERVER_HOST, SERVER_USER, SERVER_SSH_KEY and independently trusted
SERVER_HOST_FINGERPRINT; ssh-keyscan is discovery, never trust. ED25519 fingerprint,
strict host-key checking, no agent forwarding, single connection and timeouts are
retained. Actions are pinned by SHA, execution is ML/manual-only, reviewed_sha must
equal github.sha. No sudo, chmod of existing production files, new account, extra
source grants, package install on the server, Git pull or collector execution.

SAMPLES_PROJECT_PATH optionally selects an already-accessible project directory.
The compatibility default is /root/Tennis_pelmen_model, evidenced by existing
deploy/sample workflows. Nonroot users should use their existing accessible path.
By default root execution is refused; when the existing account/runtime genuinely
requires it, explicitly set allow_existing_root=true and review this necessity.
This reuses existing privileges, never obtains additional ones. The same check is
enforced on the runner, in the streamed bootstrap and in acquisition. If this
cannot be made safe, fail rather than provision accounts or expand access.
Root remains a larger trust boundary: the reviewed source and existing operator
can read privileged files, so guardrails are code-level, not an OS security sandbox.

The server's .venv supplies Python >=3.11, psycopg2-binary >=2.9, boto3 >=1.35 and
pyarrow >=15. Only reviewed code from the selected SHA is streamed to private /tmp;
deployed checkout/modules are not imported. Configuration uses the existing strict
exporter parser on explicit project/.env: flat POSTGRES_* and S3_* fields, no
get_settings defaults, DSN output or shell sourcing. storage/db.py is not imported,
avoiding its engine-on-import behavior. Actual dependency versions and PostgreSQL
major are recorded privately; config values are never returned.

The repository Compose file specifies PostgreSQL 16. Read-only/repeatable-read,
statement_timeout=5s, lock_timeout=1s and 3s connect timeout are enforced. No schema
changes, source writes, EXPLAIN ANALYZE or ingestion/service execution. Missing
explicit config, unsuitable host/container networking, dependency mismatch or
unreadable runtime fails closed. Production readiness has NOT been tested here.

## Selection, versions and unchanged load bounds

The existing algorithm reads at most four 128-row primary-key keyset pages plus
sentinels. EXPLAIN must show a bounded plain matches_pkey scan: no Seq Scan, sort,
post-filter, join or expensive plan. The repo has no migration providing a date
index, so no broad date scan is introduced. The bounded 512-row pool is filtered
locally to nonduplicate Tour/Challenger matches. Deterministic greedy diversity
uses gender, level, year, surface, numeric status, details_version and missingness;
UUID breaks ties. This selects up to 20 within a bounded UUID pool, not the newest
20, and reports shortfall rather than scanning indefinitely or inventing records.

Status/version are selection proxies, not proof of retirement, score semantics or
historical raw format. Review actual source rows and record missing diversity cases.
An optional reviewed after_uuid selects another bounded pool. First batch emits
a stable selection plan; later offsets require that exact reviewed plan.
The publication PR automatically includes acquisition-plans/<plan-sha256>.json.
Once that plan is available on reviewed ML, reuse its path. Suggested later
offset/size sequence: 0/2, 2/5, 7/5, 12/5, 17/3. Never assume separate dispatches
are one production snapshot; provenance remains per batch/object. Sample PRs
remain unmerged until the owner chooses to merge through normal review.

Exact matches UUIDs and writer-evidenced pbp_s3_path siblings cover details,
stats, PBP and raw. A reviewed supplemental manifest can include all 20 IDs and
explicit historical versions; only current-batch entries are read. For each
mentioned match/source its version list replaces derived current candidates.
No LIST, bucket scan, guessed alias join or silent newest-version selection.
All matching versions retain original sports fields/JSON strings/nulls/timestamps
and odds through the existing documented JSON codec.

Missing pointers, 404s, absent canonical_match_id columns, empty rows, truncation
and unavailable history are distinct in the report. Sibling layout does not prove
existence/completeness; pbp_completeness=1.0 is not full point-history evidence.
Other public match_id/canonical_match_id tables are inventoried as unverified
name-based candidates; their rows are not exported without reviewed indexed joins.
Thus unknown history/relations remain explicit blockers, not "all sources complete."

Per dispatch: <=5 acquired matches, <=100 exact objects/200 HEAD-GETs, no retries,
<=8 MiB/object, <=128 MiB total input, <=100000 rows/file, <=300000 decoded rows,
<=5000 retained rows per match/object/version, <=128 MiB uncompressed/file or batch,
128 columns/groups, bounded Thrift, <=1 MiB PostgreSQL JSON/row, <=32 MiB catalog
output and <=64 MiB private transport ZIP. Address space <=4 GiB; inner deadline
240s, remote timeout 260s +5s kill, SSH 290s, job 10 minutes.

Private staging permits at most two pending directories per account; stages expire
after 24 hours and cannot be released afterward. Expiry is a release check, not a
background cleanup service: use cleanup explicitly even for expired stages.
No new cron/service is installed. SIGKILL/host failure may leave private temporary
directories requiring an operator to remove only identified task directories.
Failed acquisition must not leave publicly discoverable source rows.

## Publication safeguards and exact remaining prerequisites

- Existing SSH and reviewer access must actually permit safe private inspection.
  A runner-readable SSH secret is not proof that a reviewer can inspect those
  files. If no reviewer has such access, do not check the review boxes: default
  publication is blocked until secure review can occur.
- match-sample-review must already have required reviewers, branch restriction ML
  and appropriate self-review protection. Merely naming an Environment does not
  configure it. Admins must verify those settings; this PR does not broaden them.
- The workflow must be discoverable on the default branch for workflow_dispatch,
  then executed against ML. No default-branch merge/change is performed here.
- Repository policy must allow GITHUB_TOKEN to create PRs. Only the publication
  job requests existing GitHub contents/PR write scopes; acquisition remains read
  only. GITHUB_TOKEN-created PRs generally do not trigger pull_request workflows:
  all publication validators run before upload; maintainers must arrange any separately
  required PR checks through their existing trusted process, without silently adding
  an elevated PAT or App credential. No new PAT, App key, recipient certificate or GitHub secret is required.
  If repo/org policy forbids PR creation, report the policy blocker; do not silently
  acquire an elevated token or weaken policy.
- All JSON is staged/validated locally before GitHub writes, and all review/source
  checks precede any upload. ML head must still equal the reviewed source SHA,
  checked before upload and again before sample branch creation. If ML moved,
  stop for a fresh reviewed acquisition; never reset ML or force-push.
- Existing same-ID sample snapshots must match their immutable index hashes.
  Existing sample-branch names are refused rather than force-pushed. A PR API error
  after branch creation can leave the already-reviewed public branch; inspect it
  and complete PR creation deliberately. No automatic merge/retry bypass exists.
- The publisher sends only predetermined catalog JSON paths, plan and review receipt,
  scans for secrets/contact data and its own GitHub token, and uses a dedicated
  branch from the reviewed ML tree. No .env, SSH/private key, arbitrary directory
  contents or decrypted scratch files are committed.
- Nothing automatically uploads a plaintext artifact or logs source records, even
  when publication fails. Release is digest-bound and does not re-query production.
  Actual content/rights inspection remains mandatory before selecting publish.

## Protected fallback only when private server review is unavailable

protected-match-sample-fallback.yml retains the earlier encrypted CMS delivery and
offline sample_delivery.py unpack/import-reviewed helpers as an OPTIONAL fallback.
It is not the default workflow and never automatically publishes data. It requires
a separately pinned public RSA certificate and the reviewer's private key kept
outside GitHub, as documented by its inputs/validation (RSA >=3072, exponent 65537,
valid >7 days; AES-256-GCM with RSA-OAEP/SHA-256; encrypted artifact retention 1 day).
Use it only if secure server-side inspection is unavailable and encrypted delivery
is the available review channel. Certificate creation/manual decryption is no longer
a prerequisite for the normal stage/review/publish path.

A public artifact, a public PR, a public branch, or a hash-only approval is NOT a
protected prepublication review channel. If neither private server review nor the
protected fallback is available, report that blocker and publish nothing.
