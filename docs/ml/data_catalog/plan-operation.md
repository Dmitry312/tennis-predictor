# One plan operation, one PR — Issue #81

## Standard behavior and current state

Select ML in GitHub's branch picker and run automatic-match-samples.yml. No typed
standard inputs or new approvals are added. Existing match-sample-review protection,
ML-only guard, pinned dispatch SHA/checkout, root-runtime configuration, SSH secrets,
fingerprint checks and manual final merge remain unchanged.

The recorded baseline is ML 952c817de0aebe620a9605444215acfb8e298885 after PR #80:
12 of 20 planned matches committed. Progress is read once from the persisted plan
and verified canonical index. The next logical operation reads the remaining eight
as sequential internal batches **5+3**, validates both and creates **one** final PR.
A fully unprocessed 20-ID plan uses 5+5+5+5 in the same dispatch. Tail batches contain
only the remaining IDs. A complete plan succeeds as a no-op with a short final
summary and no source access/publication.

Source access never runs concurrently. Each SSH call uses the same immutable source
code SHA and the same frozen plan, with an exact contiguous offset/count. New plan
discovery (if no prior plan exists) is allowed only in the first batch; subsequent
requests use that exact plan from a private runner file, not repeated discovery.
The payload builder validates inline plan data exactly like tracked plans. This
internal transport is not a new user input.

## Validation and publication boundary

The source exporter and SSH transport are reused. Each per-batch archive undergoes
the existing ZIP/path, UUID, standing-policy, secret/contact, provenance and checksum
checks. The operation additionally verifies exact selected plan slices, per-bundle
offset/count, report next_offset, common plan/source SHA and bounded read-usage
counters. It stores only private archives and hashes, not public intermediate data.

operation.json is written only after every requested batch succeeds. The fresh
publisher process revalidates every archive/hash/slice and the unchanged committed
index, merges entries deterministically, preserves existing files and synthetic
descriptors, and writes a single complete Git tree/commit/branch/PR. No per-batch
GitHub write exists. Both pending-PR and current ML-head checks run again before
publication. Every match retains its original file/manifest hashes. Each batch has
a distinct receipt with its original archive hash, source versions, runtime,
missing coverage and usage. One aggregate receipt records the full requested
operation, all batch references, totals and limits.

Completion means all requested IDs are represented with their explicit source
availability reports. It does NOT assert complete S3 history, perfect PBP coverage,
one database snapshot across all batches or a third-party licence. Existing owner
publication policy and all secret/contact exclusions remain unchanged.

## Explicit budgets

Per-production-batch limits are unchanged: <=5 IDs, <=100 exact S3 objects,
<=200 HEAD/GET calls, no LIST/writes/retries, <=8 MiB/object, <=128 MiB source bytes,
<=300000 decoded rows, the existing per-file/retained-row/metadata/decode limits,
read-only repeatable-read DB, 5s SQL timeout, 1s lock timeout, 3s DB connect timeout,
240s source deadline, 260s remote and 290s SSH timeout. The source records actual
calls, bytes, decoded rows and HTTP attempts without logging keys/rows.

Logical-operation caps:
- 20 planned IDs, 4 sequential production batches/connections; no parallel reads;
- 800 S3 calls/HTTP attempts, 512 MiB source bytes and 1.2 million decoded rows;
- 64 MiB maximum transport archive per batch, 256 MiB total retained archives;
- 32 MiB JSON maximum per validated batch, 128 MiB final new catalog JSON/receipts;
- 1 MiB operation metadata/frozen plan limits and exact indexed file sets;
- 512 MiB private operation storage envelope: <=256 MiB retained archives, <=128 MiB
  bounded unzip/verified-copy temporaries, <=32 MiB temporary catalog, plus small
  metadata/SSH workspace. Existing checkout storage is outside this envelope.
- runner address space <=2 GiB for acquisition/validation/publication; original
  source-process address-space limit remains <=4 GiB;
- collection deadline 1200 seconds with bounded process-group termination grace,
  each local SSH group <=300 seconds, no retries;
- final publisher <=180 seconds (+5s kill grace), workflow <=25 minutes including
  setup, preflight, collection, publication and cleanup.

Per-batch guards never grow to fit the whole plan. Exceeding aggregate budgets aborts
before publication rather than adding an unbounded loop. The final output cap may
refuse unusually large valid batches; it is not a promise that any possible source
payload fits. GitHub API errors are not retried automatically.

## Failure and safe restart

If the second batch fails after the first succeeds, no branch/PR or Git object has
been written. The private workspace is removed. The next dispatch starts again from
the committed prefix (currently 12), fetching 5+3 anew. There is no durable private
checkpoint and no partial result silently treated as committed progress. Repeated
source reads after failure are deliberate, bounded and documented. No operator
must resume individual internal batches.

The collector terminates the entire local SSH/relay/capture process group on timeout
or cancellation, not just its shell. The workflow always cleans its private operation
directory. SIGKILL/runner loss may prevent Python finally blocks; hosted-runner
disposal/always-cleanup remains the outer boundary. Source temporary-file cleanup
and hard timeouts remain as in the existing transport; no server service is added.

The sample branch is stable for source SHA + plan hash + starting prefix. An existing
branch is refused, never force-pushed. For persisted plans, preflight detects that
branch before source access. This prevents a retry from creating duplicate branches
if final PR creation failed after the complete branch was created. Inspect/complete
that existing final PR through normal recovery; it contains the whole validated
result, not a partial batch. Pending sample PRs similarly block new source reads.
A changed ML head blocks publication; start a new dispatch against the new snapshot.
A new source SHA/committed prefix is a new operation identity.

There is no cross-system transaction over GitHub HTTP calls: a failed API call after
branch creation can leave an already-complete validated branch. We do not claim to
roll back public GitHub objects. The all-or-nothing guarantee concerns the requested
data set: incomplete acquisition/validation never reaches GitHub writes.

## Isolation, rollout and evidence

Acquisition steps receive SSH credentials only; GitHub tokens are stripped from
their child environment and never go to the server. The final publisher receives
the GitHub token and no SSH key. No plaintext Actions artifacts or source-row logs.
The trusted step/runner boundary, existing Environment and permissions are unchanged.

main registration must be synchronized to the updated automatic-match-samples.yml
after normal review: PLAN_OPERATION=true, collect/publish-operation commands,
completion skip guards, private-directory cleanup, and 25-minute outer job cap.
There are no new dispatch fields. Helper code belongs on ML; the workflow still
refuses production execution from main. This implementation does not modify main,
Environment/permissions, production services, dispatch production or merge PRs.

Offline CI uses fabricated records and mocked GitHub operations, including 12→20,
full 20, tails, no duplicates, failed second batch/restart, stale ML, pending PR,
limits, checksum mutation, complete no-op and legacy fallback behavior. The project
foundation records the general rule: one logical operation → one dispatch → one
final result; internal batches are not user tasks.
