# Scalable registry bootstrap design — no population in this PR

The current private PlayerRegistry journal/snapshot is a bounded offline foundation.
It scans aliases and hashes JSON; repeatedly invoking it for millions of observations
would be inefficient. This document does not claim that the v1 implementation scales
to a production canonicalization job.

## Persist decisions before processing
- Maintain a private durable creation-event ledger with globally unique event UUIDs.
  Allocate each internal identity once as an explicit decision, persist before
  resuming work, and replay the same creation event on restart. Never generate a new
  event UUID per match, per chunk or per rerun.
- Creation IDs remain independent of normalized names. Seed only reviewed identities
  or explicitly authorized provisional candidates. Exact name agreement alone is
  not evidence that homonyms are one person.
- Preserve append-only alias/retraction decisions with actor, source reference,
  observed time (nullable), recorded time and policy versions. No mass population,
  registry migration, fuzzy merge, DB schema or source-ID invention is implemented.

## Indexed offline state
Use a local transactional index (e.g. SQLite or a columnar lookup plus embedded KV)
separate from production PostgreSQL. Proposed nonunique index:
(gender, normalization_version, unicode_version, normalized_alias, active, player_id).
Keep event_id uniqueness and an alias-event index for retractions, with reference
integrity and atomic checkpoint/event commits. Nonunique aliases preserve ambiguity.
Index exact alias candidate sets, not first-match wins; cache by pinned snapshot
digest and invalidate on corrections. Expected lookup cost O(log N + candidates),
without reserializing the entire registry for every observation.

## Bounded streaming and restart
- Pin source object/content manifests, identity snapshot and normalization/Unicode
  versions. Read projected local Parquet batches with fixed row/byte/memory limits.
- Partition by stable source identity/manifest position, not mutable player names.
  Checkpoint the exact manifest offset and last committed decision sequence.
- Batch distinct alias lookups; preserve both player orientations and unresolved
  observations. Writes should be immutable private staged outputs plus atomic
  manifest publication. Never rescan S3 or query production per match.
- Make event application idempotent; persist quarantined observations with private
  source pointers/reasons for review. Corrections append decisions and create new
  output manifests; earlier snapshots remain reproducible.

## Explicit provisional policy
Default verified training population excludes name-only, ambiguous and unmatched
identities. A research pipeline may explicitly opt into unique name-only provisional
matches, retaining flags and policy version and reporting sample counts separately.
Do not silently enable that policy in canonicalization or training. If a provisional
name bucket is used for triage, label it a candidate bucket, not a verified person;
do not collapse distinct creation decisions into it. Reviewed homonym resolution
requires explicit alias decisions. No fuzzy/transliteration/rank/photo merging.

Benchmark using synthetic journals/collision distributions and bounded local
datasets before bootstrap: throughput, memory, index size, restart idempotency,
quarantine rate, snapshot consistency and historical cutoff integrity. A separate
review must approve any real registry population or source-data export.
