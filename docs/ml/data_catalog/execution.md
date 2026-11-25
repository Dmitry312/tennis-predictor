# Bounded linked-review execution design and blockers

## What is implemented
A separate library (infra/audit/linked_record_review.py) provides:
- exact_keys(pbp_path): candidate current detail_job sibling paths across four
  S3 datasets, including raw; rejects ITF and historical/non-part layouts;
- read_matches(connection, ids): bounded exact-primary-key SELECT projection;
- ObjectReader: exact approved HEAD/conditional GET, no LIST or writes;
- build_review(...): linked source projection/local aliases;
- review_from_clients(...): isolated bounded read/project/encode orchestration.
No existing production workflow, raw exporter or artifact upload is modified.
No production connection/credentials/default config/automatic execution is added.

## Feasibility and blockers
Current detail_job writes details/stats/raw with the same gender/level/year/part suffix.
This makes bounded exact-key lookup feasible for a reviewed manifest, but is not proof
that all siblings exist or contain the selected match. PBP backfills, legacy details,
walkovers or absent PBP can lack such paths. A separate reviewed source inventory
must supply exact keys for these cases. Do not broaden to full-prefix or per-match
scans to fill gaps. Corrections/versions must be pinned by the operator's manifest;
this tool does not choose latest corrections from an incomplete object population.

SQL access is feasible through PK lookups after read-only/timed session checks. No
broad candidate query, row_to_json, name query or dynamic SQL is used. Inputs are
at most three explicitly approved canonical UUIDs and exact source object keys.
Selection for straight sets / deciding tiebreak / BO5 cannot be inferred before
score semantics are reviewed: requested cases stay explicitly unconfirmed. Choose
a bounded manifest for review and report missing sources/unknown orientation;
do not claim to have met every preferred category.

Independent winners are absent by default. Source profile names are not exported.
A reviewer must supply orientation attestations from protected inspection if player
aliases should be linked across sources. Local M1P1/M1P2 are match roles, not stable
people. No production sample has been collected or approved for this public catalog.

## Preserved or stricter limits
Compared with production_parquet_row_review.py: <=3 matches, <=12 distinct objects
(vs 18), <=24 HEAD/GET calls (vs 45 SDK calls), no LIST/pagination, <=256 KiB/object,
<=8 MiB reserved GET bytes, <=2048 rows/file, <=12000 rows total, <=64 columns,
<=32 row groups, <=4 MiB uncompressed/decoded projected bytes/file, <=6 matching
rows/match/source, <=24000 output bytes, <=65s cooperative deadline, <=95s alarm,
4 GiB address-space soft cap. Oversized objects fail; oversized raw JSON is marked
omitted without parsing. All matched-row omissions are counted. Input row order,
score values/types within the reviewed bounds and nulls are preserved.

Only HeadObject/GetObject for manifest keys/bucket are allowed, with IfMatch and exact
ETag/HTTP status/length checks. Streams close on failure. Destination must remain HTTPS
storage.yandexcloud.net; no second HTTP send per SDK call or retry is allowed.
Future client setup must use path-style addressing, TLS verification, 2-second
connect / 3-second read timeouts and total_max_attempts=1. Guards remain defense in
depth; they do not reduce a credential's IAM rights.

read_matches requires transaction_read_only=on and statement_timeout in (0,3000ms].
Use a separate SELECT-only role and an already configured local read-only transaction.
The two SELECTs are fixed and output-limited; caller owns rollback/connection cleanup.
No SET, write query, migration or catalog/service change is performed.

## Future separately approved execution
1. Review this distinct numeric-key allowlist, privacy threat model and exact output
   scope. Do not dispatch existing raw/full-match workflows for this catalog.
2. Establish private reviewed UUID/object manifest, version selection, optional
   independent winner evidence and orientation attestations; never commit it.
3. Prepare a dedicated isolated driver using the injected DB/S3 clients above.
   No such production driver/workflow is published by this PR. Use existing trusted
   SSH fingerprint verification, reviewed commit pin and protected manual approval.
4. Enforce an external hard process timeout (e.g. timeout --kill-after=2s 95s)
   in addition to library alarm/cooperative checks; native decoder calls can delay
   Python signal handling. Use a private process, not a shared interactive worker.
5. Create no persistent server output; return sanitized bytes only after complete
   projection. Do not log source payloads, exceptions or private references. The
   orchestrator raises only linked_review_unavailable and prints nothing.
6. Store returned bytes in a protected local review location, review sporting-data
   reidentification risk, and obtain separate publication approval. No automatic
   GitHub artifact upload or repository write is provided.

Blockers before production: driver/process wrapper, protected manifest, scoped
credentials/session settings, source/identity/score review and explicit user approval.
No secret values should be requested in chat. Missing coverage is not healthy data.
