> Issue #81 supersedes the per-dispatch batch behavior below. Standard dispatch now
> completes the whole remaining plan and creates one PR. See
> [plan-operation.md](plan-operation.md) for current operation limits and recovery.

# One manual dispatch → validated JSON → ML PR — Issue #73

## Normal use

Select ML in GitHub's branch picker and click Run workflow for
**automatic-match-samples.yml**. The standard operation needs no typed inputs.
GitHub may still require choosing ML; the workflow does not select a branch
automatically and refuses execution from any other branch.

Versioned workflow configuration uses github.sha for both REVIEWED_SHA and
SELECTED_SHA, explicitly checks out that exact SHA, sets BATCH_SIZE=5, and enables
ALLOW_EXISTING_ROOT=true for this specific already-existing runtime. Progress
discovery selects the persisted plan and first missing position automatically.
The legacy name REVIEWED_SHA now expresses snapshot consistency, not an independent
security review or evidence of human approval. Existing Environment approval and
branch restrictions are retained without modification.

Only two optional advanced fields remain. Leave selection_plan empty for automatic
discovery; specify it only to disambiguate tracked plans. source_manifest is for
nonstandard exact S3 history scenarios. No new required parameter, secret or
acknowledgement replaces the removed SHA/batch/root prompts.

The owner's 2026-11-26 standing permission to reuse the existing root runtime is
scoped to this workflow's bounded read-only operation. It creates no account, sudo
grant or new server privilege and changes no shared helper or fallback default.
The existing project-path configuration, SSH secrets, fingerprint and execution
guards remain. All risk controls, including manual merge, still apply.

One dispatch computes the remaining plan → sequentially reads/validates bounded
PostgreSQL/S3 batches → creates
a dedicated stable samples/first20-plan-<operation-hash> branch and PR into ML. No repeated
content_reviewed/content_evidence/rights_reviewed/rights_evidence inputs or substitutes,
manual certificate, archive download, import or separate publication dispatch.
The project owner's standing decision is recorded once in publication-policy.json
and ../real-data-review-policy.md. It permits ordinary original sports records;
it does not establish any third-party licence. Content eligibility remains the
owner's responsibility; no manifest or receipt claims legal review/certification.

Each internal source call returns a bounded ZIP through pinned SSH, directly into a private
0600 runner file. There is no plaintext Actions artifact or raw-row log. The
runner verifies the exact ZIP file set, schemas/manifest hashes, provenance,
source SHA, plan hash, batch count and automatic secret/contact guards before any
GitHub content write. Original names, IDs, JSON strings/keys/values, results,
timestamps, nulls and odds are retained. No anonymization or invented source data.

Publication reuses the immutable catalog merger, preserves synthetic entries and
existing samples, adds separate matches/details/stats/PBP/raw/manifest JSON files,
persists the stable plan and a policy/validation receipt. Existing new-ID conflicts,
already-catalogued IDs, changed ML head or a pending sample PR abort rather than
overwrite or force-push. No automatic merge occurs. Runner transport bytes are
removed on success/failure; the default path creates no persistent server review stage.

## Exact PR #72 migration and first-20 continuation

Observed existing successful runs: staging #45770870231 and publication #45773343170
created PR #72 with two matches. Its head at inspection was
950a5bf12fdc4cd7c888ef94d4ed6356ed83dc12. It was pending during Issue #73;
by Issue #76 it is merged and its two examples are on ML. The persisted plan is:

    docs/ml/data_catalog/acquisition-plans/2c161258d768f17a129cb5643754f972524a2f2ea32f5104a23b23da8a44d34c.json

It contains 20 selected IDs, no shortfall. This implementation changes none of
PR #72's sample files, branch or review metadata, and does not merge it.

A coherent canonical index cannot include PR #72's entries before their sample
files exist on ML without dangling links; copying those files into another PR would
duplicate its data. Consequently the default checks open samples/first20-* PRs
BEFORE SSH and blocks while #72 is pending. It does not silently pick another plan.
Closing the known baseline PR without incorporating its catalog also does not
silently rediscover the first two samples.

The baseline migration is already complete; do not merge or import PR #72 again.
After the standard workflow version is incorporated normally, dispatch discovers
the tracked plan, verifies committed samples/checksums and resumes at the current
first missing position (2 when only the original two examples are present). If other plans also exist, set the
optional selection_plan path above; it is a path selector, not a new approval.
No offset, ID list, archive extraction or rights statement needs to be supplied.
There is no unsafe override that publishes a conflicting index while #72 is pending.

One invocation processes every remaining contiguous slice, in batches of at most
five. With 12/20 already committed, the internal batches are 5+3, followed by one
final PR. These are not separate user invocations.
Only the single final sample PR requires normal incorporation. Internal batches
never create PRs and never require operator participation.
The workflow never merges a predecessor automatically. Once all 20 selected IDs
are catalogued it reports first20_complete before source access. Noncontiguous or
ambiguous catalogs fail closed; the optional path disambiguates an existing plan.
A genuine bounded-pool shortfall stays explicit rather than inventing more IDs.

## Existing infrastructure, isolation and permissions

Reuses SERVER_HOST, SERVER_USER, SERVER_SSH_KEY, SERVER_HOST_FINGERPRINT and the
already-accessible server Python/config. No new accounts, source grants, secrets,
certificates, Docker service, server installation, sudo or production deployment.
The existing SAMPLES_PROJECT_PATH/compatibility path remains; runtime reuse is
configured by the standing permission above, with no per-run root prompt.
The existing match-sample-review Environment is retained unchanged: its execution
protection is not a new replacement for removed content/rights fields. This task
does not inspect or modify administrative protection settings.

The one workflow has one reviewed trusted runner job, with minimal contents:write
and pull-requests:write needed for its final purpose. Credentials are separated
by step environments: the read-only GitHub progress lookup and final publisher
receive GitHub token; the SSH step receives only the four existing SSH secrets.
Git credentials are not persisted by checkout; SSH key files are deleted by the
existing transport trap. GH_TOKEN/GITHUB_TOKEN are explicitly removed before
SSH, never serialized into the reviewed payload or sent to the server. The
publisher step receives no SSH credentials. This is process/step separation on
the SAME trusted runner, not a claim of separate-VM isolation. Only the exact
selected ML code snapshot and SHA-pinned actions execute; data is parsed as bounded JSON,
never executed as code. No untrusted PR checkout is used for source access.

No cross-job plaintext artifacts, extra transfer secrets or ephemeral certificate
provisioning are needed. The original optional two-step workflows remain available
for an operator who chooses protected staged review or encrypted fallback.

## Source limits and diagnostics are unchanged

The PostgreSQL 16 fix from PR #71 orders by the qualified UUID column, not its text
output alias. Candidate discovery remains <=4×128 rows plus sentinels, no full
scan/sort/post-filter plan, at most 20 selected, standard 5/max 5 acquired. Exact
UUID joins and a stable persisted plan prevent duplicate/resampled first batches.

DB remains read-only repeatable-read with 5s statement timeout, 1s lock timeout,
3s connect timeout. S3 remains exact HEAD/conditional GET, no LIST or writes,
no retries, <=100 objects/200 operations, <=8 MiB/object, <=128 MiB input.
Decode bounds remain 100000 rows/file, 300000 total, 5000 retained per
match/object/version, 128 MiB uncompressed/file or batch, 128 columns/groups,
bounded Thrift. PG JSON <=1 MiB/row, catalog <=32 MiB, transport <=64 MiB.
Per-source-process address space <=4 GiB; per-batch deadlines remain 240s inner,
260s remote +5s kill and 290s SSH. The whole operation has a 1200s collection
deadline, <=2 GiB runner address space and a 25-minute Actions job; see
plan-operation.md for aggregate byte/request/storage caps. No new queries broaden these budgets.

All explicit source versions, content hashes and original records remain separate.
Missing pointers, objects, joins, rows, raw data, truncation and unknown history
are reported. The tool cannot discover all S3 history without an evidenced manifest;
unknown other PostgreSQL links remain inventory, not exported guesses.
pbp_completeness=1.0 still does not prove complete point history.

PR #71's fixed allowlisted diagnostics and stderr bounds remain. Progress and
automatic publication add fixed stages; no exception strings, credentials, private
paths or source rows appear in logs. Automatic checks reject passwords, access
tokens/keys, private keys, credential URLs and unrelated contact fields including
embedded JSON. They cannot legally certify a source or perfectly classify all
free text; the owner is responsible for eligibility decisions. Synthetic tests
prove implementation behavior, never production score semantics.

## Remaining operational prerequisites and limitations

GitHub requires workflow_dispatch registration on the default branch before
choosing ML; this PR does not perform that registration merge. Existing runtime,
source permissions and Environment protection must work as before. The execution
SHA is selected automatically from the dispatch snapshot, not entered by the operator.
No production dispatch or permission change was performed by Codex.

Repository/org policy must allow GITHUB_TOKEN to create PRs. No PAT or new App
secret is introduced if policy forbids it; report the blocker instead. PRs created
with GITHUB_TOKEN generally do not trigger pull_request CI. Integrity/content
validators run before upload in the automatic workflow; maintainers must arrange
any separately required PR checks through their existing trusted process. No
new production or merge workflow is dispatched to work around that limitation.

Open sample PRs serialize catalog changes. A publication API failure after branch
creation can leave an already validated sample branch; existing names are refused,
never force-pushed. Inspect that branch and complete its PR through normal recovery.
No source records are uploaded until automatic checks pass, and no actual records
were acquired or published while implementing this change.


## Exact registration update for Issue #76

The repository default branch is main. At implementation time main already held
automatic-match-samples.yml with the old five-input form. Through normal review,
update that same file on main to the exact version in this PR: retain only the
optional selection_plan/source_manifest inputs, set both SHA variables and checkout
ref to github.sha, and set workflow BATCH_SIZE='5'/ALLOW_EXISTING_ROOT='true'.
Keep the ML-only guard, match-sample-review Environment and other controls identical.
A stale main registration can otherwise keep showing obsolete required form fields.

The helper code need not be copied to main to register the form: production execution
is allowed only when ML is selected and its exact dispatch SHA is checked out. The
matching workflow version must also be incorporated into ML before standard use.
This task does not update main, merge a PR, change Environment settings or dispatch
production. See ../foundation.md for the project-wide one-click operation convention.
