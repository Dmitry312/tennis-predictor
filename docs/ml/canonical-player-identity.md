# Offline canonical player identity foundation v1

Library: src/tennis_predictor/ml/player_identity.py. No production IO, configuration
imports, network calls, migrations or collector changes. No production data is seeded.
ML scope remains Tour/Challenger singles; odds, rankings and photos are not identity keys
or model features introduced here. Evidence is recorded in identity-audit-evidence.md.

## Registry contract

PlayerRegistry is an explicit single-writer append-only decision journal.
apply(event) accepts only create, alias and retract with a globally unique canonical
UUID event_id and complete provenance: source, reference, actor, observed_at
(nullable when unknown), recorded_at (timezone-aware). Observed time cannot exceed
recorded time; recorded_at is nondecreasing in journal order. Journal order is the
replay order, not arbitrary sorting by match date or name.

create includes gender M/F and an initial name. It always creates an independent
internal UUID5 from the fixed ID-version namespace and creation-event UUID, never
from normalized name, source match ID, ranking, photo or record order. A second
creation with the same name creates another ID and hence an ambiguity; it does
not merge people. Stable IDs survive alias additions/retractions and snapshots.
Calling code must persist creation events and never regenerate them per replay.

alias explicitly binds a name to an existing internal ID, with its own provenance.
Unknown IDs fail. Aliases with the same normalized name attached to multiple IDs
remain present and ambiguous, never first-match-wins. retract explicitly withdraws
one alias decision; it cannot delete or renumber a person. Correct a mistaken alias
by retracting it and adding a new alias event. There is no automatic merge, split,
reassignment, source-ID verification or ranking-based lookup operation.

Same event_id and identical content is idempotent at apply(); different content is
an error. replay() rejects duplicate journal entries and unknown/out-of-order
references. Validation and size checks precede state mutation. events returns a
deep copy; historical snapshots are not changed by later corrections.

## Conservative matching and quarantine

Normalization v1: Unicode NFC, lower(), trim/collapse whitespace. Preserve accents,
punctuation, initials, name order, scripts and sharp-s (no casefold expansion).
No transliteration, punctuation stripping, accent removal, token sorting, edit
distance or fuzzy matching. Controls/format characters, empty/non-letter names,
doubles slash and oversized names are rejected conservatively. Unicode database
version is pinned in snapshots to prevent silent behavior changes across runtimes.

resolve(gender, name) does not mutate the registry:
- exactly one active candidate -> provisional / name_only, verified=false;
- zero candidates -> quarantine / unmatched_name;
- multiple candidates -> quarantine / ambiguous_name, no selected player_id;
- missing/unknown gender or invalid name -> quarantine, never a wildcard search.

Gender is required context, not inferred from name or ranking. Normalization collisions
and genuine homonyms cannot be distinguished automatically. Even a unique alias
is not a verified real-world identity; provisional matches must not silently enter
a verified training population. An internal ID is a registry handle, not proof.

Resolution records include revision, registry digest, normalization/Unicode versions
and candidate IDs. Persist a quarantined result with a private observation reference
in the caller's future canonicalization layer; this foundation does not read or
rewrite match records. No observation reference, player name or source payload is
automatically logged. There is no production quarantine table/service in this PR.

## Replay, persistence and temporal use

to_json()/from_json() provide versioned private snapshots with schema, ID,
normalization and Unicode versions, original events and a SHA256 digest.
Unknown versions, duplicate JSON keys, malformed events and checksum mismatch fail.
The digest detects accidental corruption; it is not an authentication signature or
proof that provenance claims are true. Restrict access and review event sources.

save(path)/load(path) are explicit **offline local** persistence APIs. save creates
a 0600 temporary file in the same directory, fsyncs, atomically publishes a hard link
without replacing an existing target, then cleans up the temporary file; it fsyncs
the directory on Linux. Unsupported filesystems/permissions fail rather than overwrite.
The directory must be private and support hard links. A failure after publication
can leave a valid target; load/verify it rather than overwrite. No default path,
automatic loading, CLI, cloud upload, secret or production path is configured.

Limits: 10000 events, 16 MiB snapshot, bounded provenance and names. This v1 rebuilds/
hashes JSON in memory; it is an offline foundation, not a high-throughput state engine.
Snapshots contain private names/provenance; never print or upload real snapshots in CI.
Only synthetic fixtures are used in tests.

at_revision(n) replays a historical prefix; prior resolution records remain tied
to their digest/revision. Use a pinned reviewed snapshot at a prediction cutoff:
latest identity decisions may contain later knowledge. Provenance observed_at unknown
must not be fabricated, and this foundation alone does not establish strict historical
available_at. No feature replay or dataset generation is authorized here.

Example (synthetic only):
1. Create an empty registry and apply a create event with caller-owned event UUID.
2. Persist its returned internal ID and the private snapshot locally.
3. Add independently reviewed aliases using explicit events/provenance.
4. Resolve observations; quarantine ambiguous/unmatched ones for separate review.
5. Pin snapshot digest/revision in a future canonical manifest, never assume verified.

The [owner review workflow](player-registry-review.md) adds disk-backed
historical staging and explicitly reviewed match-side mappings. It reuses the
normalization and opaque creation principles above while keeping candidate
grouping separate from the existing small append-only registry contract.
