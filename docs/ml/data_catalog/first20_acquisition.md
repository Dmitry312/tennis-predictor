# Reviewed first-20 acquisition — Issue #67

This change prepares acquisition; it does **not** execute it. No production
connection, source records, schema observations, workflow dispatch or merge were
performed. All tests use fabricated records. The owner approved public original
sports data on 2026-11-25, not credentials or unrelated personal information.
Content and redistribution-rights review still precede a deliberate Git commit.

## Repository evidence and runtime readiness

- scripts/export_match_samples.py supplies exact joins, JSON codec, source versions,
  read-only checks, S3 guards, budgets, immutable bundles, index publication and reuse.
  It is reused unchanged. The older traceback-printing production driver is not used.
- src/tennis_predictor/config.py exposes flat postgres_* and s3_* fields. storage/db.py
  constructs a SQLAlchemy engine on import; acquisition never imports that module.
  It uses the existing strict exporter config parser on the explicit project .env,
  without sourcing shell, printing settings, guessing defaults or reading root files.
- Existing sample/identity workflows demonstrate SERVER_HOST, SERVER_USER,
  SERVER_SSH_KEY, SERVER_HOST_FINGERPRINT and pinned ED25519 verification.
  The new workflow reuses this mechanism, with no root SSH or sudo.
- deploy.yml is disabled and refers to /root/Tennis_pelmen_model; it does not establish
  that a nonroot checkout/virtualenv is accessible. The new explicit project path is
  a mandatory operator prerequisite, not something this PR provisions.
- pyproject.toml requires Python >=3.11, psycopg2-binary >=2.9, boto3 >=1.35,
  pyarrow >=15. The deployed .venv is used only for dependencies. Reviewed code is
  streamed from the exact selected ML SHA into private temporary files; no server
  Git pull, package install, checkout change, collector execution or service restart.
- infra/docker/docker-compose.yml specifies PostgreSQL 16. The future run records
  actual PostgreSQL major, Python and package versions in the encrypted report.
  Runtime versions/config availability have NOT been verified against production
  during this PR. Missing/incompatible dependencies or incomplete explicit settings
  fail closed. Container-only dependencies or a Docker-only DB hostname cannot be
  repaired by host SSH; provision a reviewed nonroot execution context separately.

## Deterministic bounded selection

No migration creates a match_date index. A broad date filter plus LIMIT could
still scan/sort a large matches table. Instead, acquisition reads at most four
pages of 128 rows, with one extra sentinel per page, ordered by UUID primary key.
Subsequent pages use parameterized match_id > cursor, never OFFSET. Only plans
consisting of LIMIT and a plain matches_pkey Index Scan/Index Only Scan are allowed.
Seq Scan, sorts, post-filters, joins and expensive plans are refused before SELECT.
Every SQL statement times out at 5s in a read-only repeatable-read transaction.

The 512-row pool is filtered locally to nonduplicate Tour/Challenger records.
Greedy selection maximizes new observed gender, level, year, surface, numeric status,
details_version and missingness dimensions, breaking ties by UUID. This chooses
the first **up to 20 diverse matches in a bounded UUID pool**, not the oldest/newest
20 and not a representative population sample. An optional after_uuid seeds another
reviewed bounded pool. No attempt is made to keep scanning until enough cases exist.
Insufficient candidates produce a counted shortfall, not fabricated records.

Status/details_version are selection proxies. They do not prove a retirement,
walkover, score mapping or raw historical score format. Exact source schemas and
original rows are exported for review; the report leaves score_formats unverified.
If the bounded pool lacks a required case/year/format, the review must record that
gap and choose another bounded pool or an independently evidenced UUID plan.
There is no promise that all requested diversity dimensions occur in the database.

## Exact source linkage and honest coverage

For each batch, exact UUID lookup rereads full matches rows and metadata. It rejects
present records that have left Tour/Challenger scope or become duplicates; missing
PG rows remain explicit. A validated pbp_s3_path suffix supplies current candidates
for pbp, nbbet_details, nbbet_stats and nbbet_raw, consistent with detail_job.flush.
Every source uses the existing HEAD/If-Match GET, version/size/hash verification,
no LIST calls, and exact canonical_match_id row joins. Ordinary sports identities,
original JSON strings/keys, nulls, timestamps and odds remain unchanged.

An optional reviewed exact source manifest supplies historical keys/versions or
paths for matches lacking a pointer. It may cover all 20 selected IDs and is
restricted to the current batch before reads. For each mentioned match/source,
its explicit version list replaces the automatically derived current candidate;
include current and historical versions explicitly if both are wanted.
The same exact manifest can be reused across later batches. Unknown selected IDs
or other prefixes are rejected.

Without a pointer or independent exact manifest, it is impossible to discover all
historical details/raw/stats/PBP objects without an index or wider S3 scan. No such
scan or history guess is added. Missing pointers, object 404, absent join columns,
empty rows, row truncation and unplanned history remain distinct. The encrypted
report lists each source's rows, coverage, schemas and omissions. PBP completeness
is never established by has_pbp, ingestion state or pbp_completeness=1.0.

The existing exporter inventories public match_id/canonical_match_id table names.
These are candidates, not verified FKs; their rows are not exported. Arbitrary
related PostgreSQL tables require separately reviewed indexed joins and a content
policy. Invisible tables/differently named links remain unknown. Thus "all sources"
means all four evidenced repository source prefixes plus matches, with other
PG/history gaps explicitly reported, not silently declared complete.

## Conservative batches and stable resume

One manual run exports **2 by default**, maximum 5. The first run selects the
20-ID plan and exports its first batch. It delivers selection.json with the entire
stable plan and acquisition-report.json alongside that batch's catalog. Later runs
must use that reviewed plan from docs/ml/data_catalog/*.json. Offset > 0 without
a plan is rejected; selection is not repeated independently between batches.
Every bundle/report records the plan SHA-256. Object versions/PG snapshots may
differ between runs and remain recorded rather than pretending all batches are
one production snapshot.

Suggested future sequence, only after explicit authorization to execute:
offset/size 0/2, 2/5, 7/5, 12/5, 17/3. If the plan has a shortfall, stop at the
actual selected count. Download/review the first small encrypted batch before
approving later batches. Add the reviewed selection plan to ML via the normal PR
process before resume; it contains permitted sports metadata, not credentials.

Budgets per dispatch:
- candidate pool <=512 rows plus sentinels; <=20 selected, <=5 acquired;
- <=100 exact objects, <=200 HEAD/GET operations, no SDK retries;
- <=8 MiB compressed per object, <=128 MiB total input;
- <=100000 rows/file, <=300000 total decoded, <=5000 retained per match/object;
- <=128 MiB uncompressed/file or batch, 128 columns/groups, bounded Thrift;
- <=1 MiB JSON/PG row, <=32 MiB exported catalog, <=64 MiB ZIP delivery;
- 240s inner alarm/deadline, 260s remote timeout with 5s kill grace,
  290s SSH timeout, 10-minute Actions job; process address space <=4 GiB.
These supplement, rather than remove, the exporter hard ceilings.

## Mandatory setup before any dispatch

1. Make the workflow discoverable on GitHub's default branch through normal review.
   GitHub workflow_dispatch requires the workflow to exist on the default branch;
   choose ML when running it. No default-branch change or merge is part of this PR.
2. Configure Environment match-sample-review with required reviewers, prevent
   self-review where supported, and allow only ML deployments. YAML naming an
   Environment alone does NOT create approval protection. A repository admin must
   verify these settings; the workflow cannot prove them through its read-only
   token. No secrets reach a pull_request job.
3. Reuse the four SERVER_* secrets in that Environment/repository. Require an
   independently verified SHA256 ED25519 host fingerprint. ssh-keyscan discovers
   a key but never establishes trust by itself. Root SERVER_USER is rejected.
   Provision a nonroot account with access only to the necessary existing config,
   runtime and read-only sources; do not chmod root files or grant sudo.
4. Set Environment variable SAMPLES_PROJECT_PATH to the approved absolute nonroot
   project directory, containing readable .env and executable .venv/bin/python.
   Safe characters only, no .. segments. No automatic path/root discovery.
   Use SELECT-only PG and exact HEAD/GET S3 credentials. Explicit settings required
   by the exporter may need an operator-maintained read configuration.
5. The reviewer generates/keeps an RSA private key locally outside Git, preferably
   in a protected key store. Publish only its matching PEM X.509 certificate as
   SAMPLES_REVIEW_CERT_PEM and its SHA-256 DER certificate fingerprint (64 lowercase
   hex characters, no colons) as SAMPLES_REVIEW_CERT_SHA256. Key size >=3072,
   exponent 65537, certificate currently valid with >7 days remaining.
   Review/pin the certificate fingerprint independently. Never put the private
   key in GitHub Secrets, Actions, source manifests or this repository.
6. Verify runner OpenSSL supports authenticated CMS AES-256-GCM and recipient
   decryption before acquisition. Offline CI tests the same roundtrip/tamper path.
   cryptography==44.0.3 is installed only on the runner for certificate validation;
   no new server package is installed. Python >=3.11 and deployed source dependency
   versions are checked remotely without printing configuration.
7. Inspect exact ML code and enter its full 40-character reviewed_sha. It must
   equal github.sha; workflow execution is limited to ML/manual dispatch. Actions
   are pinned by full SHA. Changing the Environment variables/secrets after review
   remains an administrative trust boundary.

## Encrypted delivery, review and local integration

Raw sample bytes pass through SSH stdout directly into the sealer's bounded memory.
They are never printed or persisted as a plaintext Actions artifact/file. The runner
uploads only samples.cms, standard authenticated CMS AuthEnvelopedData using
AES-256-GCM and the pinned RSA recipient certificate. A 1-day retention limit is
additional minimization, not a confidentiality boundary: public-repository artifacts
can be downloadable by users with repository read access. Encryption is mandatory;
there is no plaintext fallback. Package sizes and run metadata remain visible.

Authentication failures, deadlines, invalid certificates/settings and source errors
emit fixed stage-independent diagnostics. Neither remote stderr, tracebacks, source
records, config, private keys nor credential values are shown. Runner temporary SSH
keys, candidate host keys, certificates, payload and encrypted staging are cleaned.
Remote ordinary completion/errors clean private code/catalog temporary directories;
SIGTERM is handled for cleanup. SIGKILL, host failure or disk failure can leave
private unindexed /tmp directories; an operator may need to remove only those
identified task directories. No production persistent data/service is modified.

Download the encrypted package from the expected successful run. Decrypt locally:

    python scripts/sample_delivery.py unpack \
      --input /protected/download/samples.cms \
      --private-key /protected/reviewer/private-key.pem \
      --output /protected/review/first20-batch0

The private key must be locally usable by OpenSSL (the helper does not prompt or
take passwords on the command line). If the key is encrypted, use the reviewer's
normal secure key-unlocking process outside Actions; never place a plaintext key in
the repo. Authentication must succeed before ZIP extraction. Extraction validates
exact file names/counts, sizes, duplicate names and regular-file types, rejects
traversal/symlinks/unindexed files, and verifies catalog checksums using reuse().
The delivery is encrypted, not digitally signed; obtain it from the trusted expected
workflow/run, not an arbitrary CMS file encrypted with the public certificate.

Review original sports records and source redistribution rights. Compare known
results with details/raw/stat/PBP; leave numeric keys/score semantics UNKNOWN until
supported by observed evidence. Check missingness and actual diversity, not just
the PG proxies. Keep first20 plan/report as evidence. Do not treat synthetic test
fixtures as real cases.

Only after that actual review, integrate locally without replacing the catalog:

    python scripts/sample_delivery.py import-reviewed \
      --input /protected/review/first20-batch0 \
      --output docs/ml/data_catalog --content-reviewed --rights-reviewed

This uses the existing immutable publisher, preserves original bundle hashes,
merges index entries and retains the synthetic index. It never runs git or pushes.
Conflicting existing snapshots fail rather than overwrite. Review booleans attest
actual content/rights inspection, not a second blanket approval of sports identities.
Record the review evidence in the sample PR; immutable manifests retain pending
export-time review state. Add the reviewed plan and report deliberately under
docs/ml/data_catalog with descriptive names, then commit reviewed sample files/index.
Do not commit .env, private keys, source access configuration or decrypted scratch
directories. Offline reuse continues to work without production access.

## Outstanding blockers before the first actual run

Environment protections/default-branch discoverability, a nonroot readable runtime,
complete explicit config, source read permissions, OpenSSL/recipient interoperability,
actual database plans/statistics, exact history manifests and source redistribution
rights are unverified against production. This PR supplies checks and reports,
not proof those prerequisites are met. No actual 20-match dataset exists yet.
If current plans are refused, do not disable safety gates or automatically add an
index: review an execution plan/change separately. Gaps in historical lookup or
other PG relations must remain explicit until evidence supplies exact safe joins.
