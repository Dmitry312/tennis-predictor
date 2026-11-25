# Canonical match/result contract v1 — Issue #60

Offline library: src/tennis_predictor/ml/canonical_match.py. No DB/S3/network reads,
writes, production jobs, migrations, training or feature generation. Inputs are
explicit local Match metadata rows, details writer rows, source provenance,
an optional reviewed score mapping and one pinned PlayerRegistry snapshot.

## Evidence inspected
Base ML: 2a8751d6e313421cd42dbb9fb18c0b1b6ba872ac.
- storage/models.py: Match UUID/source IDs, gender/level, names, nullable surface,
  naive-UTC start_ts, duplicate_of, status and details_version. There is no winner column.
- details.py: details_to_rows serializes source sets into sets_json without a
  semantic score-key mapping; players_json uses p1/p2 objects with name at "1".
- status.py distinguishes FINISHED=3 from retired=9, walkover=8, awarded=10 and others.
- infra/audit/match_score_validation.py accepts caller-supplied keys and compares a
  majority of non-tied set scores with independently supplied winners. It does not
  establish legal terminal games, BO3/BO5 completion or the production key meanings.
  It remains an offline hypothesis-review helper and is not used as a target extractor.
- docs/manual-parquet-data-dictionary.md is a review protocol; inventory explicitly
  says score keys, orientation and formats still require verification. Anonymous
  K1..K8 fields from row review are not stable source keys.

Therefore v1 never guesses numeric sets_json keys, winner fields, format from gender,
or independent winner evidence. Without ScoreContract the target is unavailable.
Tests preserve a synthetic explicitly mapped score shape through the real
MatchDetails/details_to_rows writer; synthetic names/keys do not assert a production
mapping. No source-score semantic review or production target coverage is claimed.

## Canonical record
CanonicalMatch stores canonical/source match IDs; Tour/Challenger gender/surface/
tournament context; UTC source event time; metadata/result availability; ingestion
time; source reference/content SHA; details version/status; ordered internal players;
source-to-canonical orientation; identity digest/revision/provisional/quarantine flags;
validated canonical-oriented sets with games/points units; separate Target; and
contract/mapping versions. Records and nested contracts are immutable dataclasses.

Caller provenance must pin a private local row/object reference and object content
hash. The library validates structure, not authenticity, and does not fetch content.
Raw names, odds, rankings and raw payloads are not carried in canonical output.
References may themselves be private; never log real records or source pointers.

Input join requires matching metadata.match_id/details.canonical_match_id.
If supplied, nbbet_match_id must agree. duplicate_of must be explicitly present;
non-null duplicates are excluded, never collapsed by player pair/date. Repeat
meetings and separate IDs cannot safely be merged by names or nearby timestamps.
Multiple canonical IDs sharing one source ID are excluded as source_id_collision.

## Players and orientation
Resolve both Match names against one pinned gender-aware identity snapshot.
Every current unique name-only mapping remains provisional and
verified_training_eligible=false. Ambiguity, unmatched identities, same-player
mapping, conflicting statuses/source IDs or ambiguous orientation quarantine target.
There is no verified source-ID assertion or automatic identity merging.

Orientation compares both documented details p1/p2 names with both Match names
using the existing conservative normalizer. Exactly direct -> same; exactly reverse
-> swapped, including score/tiebreak inversion. Missing names, aliases that do not
match the source names conservatively, homonyms or contradictory names do not
authorize guessed orientation. Source sets are mapped in source P1/P2 order first.

## Score contract and target
ScoreContract names P1/P2 games keys, optional paired tiebreak-point keys, best_of
(3/5), deciding-set policy, evidence reference and source-schema mapping version.
Evidence is an external review requirement; a nonempty string alone is not proof.
The caller must review applicability to the exact input schema/event. No default
mapping is provided. All optional mapping-key pairs must be distinct.

Only status 3 can produce P1-win (1/0). Count legal complete sets until exactly
2 wins in BO3 or 3 in BO5. Reject incomplete/tied/negative/fractional/bool/string
scores, illegal terminal games, extra sets after victory and unknown formats.
Mapped scores are retained so tied-version corrections with different scores
cannot be collapsed just because the winner agrees.

Policies are explicit conventional scoring definitions, not inferred production facts:
- tiebreak7: ordinary sets 6:0..4, 7:5 or 7:6;
- tiebreak10: same deciding-set game endpoints, but supplied deciding tiebreak
  points must terminate at >=10, win by two;
- advantage: deciding set 6:0..4, or >=7 with exactly two-game margin; no 7:6;
- match_tiebreak10: BO3 only, third set interpreted explicitly as points to 10,
  win by two, never as games. Earlier sets remain ordinary tiebreak sets.

Non-deciding sets use ordinary 7-point tiebreak rules. Observed optional tiebreak
points must agree with the game winner and legal point termination; missing
tiebreak-point detail is not invented. This validates terminal score at game/set
level, not PBP completeness. Score values are bounded <=99; unsupported historical
12-all/short-set/other formats require a new reviewed policy, not heuristic parsing.

Target.origin=score_derived and independently_verified=false always. No independent
winner field is fabricated or inferred from odds, ranking, PBP point_winner or names.
Statuses 1/2/4/5/6/7/8/9/10 have no V1 target. In particular retirements, walkovers,
awards and canceled matches are excluded even if their partial score looks decisive.

## Time and leakage
Known writer naive UTC start_ts and fetched_at are explicitly converted to UTC.
start_ts is the source's start timestamp, not independently proven actual start.
Unknown stays None: no fallback from match_date to midnight, from fetched_at to
available_at, or from start_ts to completion/availability time. Explicit availability
requires timezone-aware input. Inconsistent pre-start result availability or
availability after ingestion quarantines the record.

canonical_batch(as_of=...) requires known metadata and result availability by cutoff
and a registry whose decisions were recorded by cutoff. Unknowns/later versions are
excluded with reason counters, never silently treated as strictly available.
Availability is caller-provided evidence, not independently verified by this library.
Ingestion time orders revisions but is not itself a proof of historical availability.

prematch_context is an allowlisted context projection gated by metadata availability,
event time, cutoff and the exact historical registry snapshot. It contains no score,
target, result status, odds or rank, and does not use current-result acceptance to
select fields. It retains the provisional-identity flag. It is not a feature store
or an authorization to train; all current records remain ineligible for a verified
training population. Later identity knowledge must not rewrite earlier replay.

## Deduplication and corrections
canonical_batch accepts at most 10000 local observations and returns records sorted
by match UUID plus fixed reason counters. For one match:
- known fetched_at -> latest ingestion version;
- latest-time identical canonical payload -> one deterministically selected reference;
- latest-time conflicting payload -> correction_conflict, no chosen record;
- unknown version ordering + differing payload -> unordered_correction;
- byte/provenance differences with identical canonical payload can collapse; the
  lexically smallest content hash/reference is a deterministic provenance tie-break.
Source object hashes are asserted by the caller. No ingestion filename or winner is
used to choose a contradictory tied version. Retain previous manifests/references
for reproducibility; corrections do not mutate earlier records. A latest invalid
target is not replaced by an older convenient valid label. Any metadata-invalid observation with a recognizable match UUID conservatively blocks
that entire match group (invalid_correction_quarantine), preventing fallback to an
older valid label. Availability exclusions in explicit as-of mode instead allow a
known earlier version; rejected inputs without a recognizable UUID cannot be joined.

## Verification and limits
CI includes canonical player identity tests, identity audit regressions, the old
score-review helper and new writer-compatible/edge/leakage/correction tests.
No production target is generated here. Remaining gates: real score-key/format
mapping review, verified player identities, availability provenance and bounded
offline export. Registry scaling design is in registry-bootstrap-design.md.
