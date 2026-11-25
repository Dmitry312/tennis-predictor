# Player identity audit — recorded evidence

## Confirmed bounded observation

Source: existing production run https://github.com/Dmitry312/tennis-predictor/actions/runs/45660651682/job/139369270886
at reviewed ML SHA 30a925585834935e154d489bfbd1ebed1de8f61a, 2026-11-25.
This document was prepared by reading the existing aggregate job output, not by
running another production audit or reading S3/DB.

| Aggregate | Observed |
| --- | ---: |
| decoded rows | 833 |
| invalid JSON rows | 13 |
| unique source/year rows | 820 |
| details player-object denominator | 876 |
| raw match-player-object denominator | 764 |
| compatible files / GET attempts | 20 / 24 |
| LIST calls | 32 |
| accounted bytes | 5578506 |

bounded_scan_finished=true, complete=false, schema_compatible=false,
identity_confirmed=false, population_coverage=unknown.
Four selected details objects, one in each 2026 gender/level stratum, lacked
players_json; all 12 selected raw strata were compatible. A nonzero exit reflects
the explicit schema incompatibility policy, not a failure to finish the bounded scan.

Observed year partitions span 2016–2026; selected years were 2016/2021/2026.
Eight emitted path/scope combinations were the known name/photo and ranking
fields (details.player and raw.match.player, field_1, field_2,
field_4.field_1, field_4.field_2). The source schema also documents a flag at
player field "3"; it is not evidence of an ID. No additional ID candidate was
established in the inspected sample. These counts are not distinct-player totals.

## Coverage and unknowns

This is not proof that stable source IDs do not exist. Samples are restricted
to small, lexically selected files and three observed years, with incomplete
schema coverage. Thirteen invalid JSON rows supply no identity evidence.
No aliases, real names, IDs, object keys, photos or payloads are reproduced here.
Missing columns and source/year deduplication limit coverage; repeated payloads
do not prove independent repeated-player observations. A stable source ID,
cross-year identity equivalence and exhaustive outside-match mapping coverage
remain unknown.

## Repository inspection (no production access)

- discovery.py:_fetch_day/discover_range exposes listing fields "7"/"15" as player
  names, "3" as match slug, and extracts the leading slug digits as nbbet_match_id.
  That is a **match ID**, not a player ID. NbBetCandidate has no player-ID fields.
- results.py:_fetch_day/run uses the same name fields and match-ID extraction.
  This is code inspection, not proof about every unused listing field.
- details.py:parse_match_page/details_to_rows preserves match["3"]/["5"] objects
  in players_json and the full pageTennisEvent in raw_json. Its documented fields
  are name, photo, flag and ranking. Ranking changes over time; photo URLs and
  names cannot be promoted to stable IDs.
- Existing raw audit traverses mappings outside match. Its bounded observation
  found no extra candidate; unobserved or differently shaped mappings remain unknown.
- PostgreSQL model player1/player2 names and numeric JSON schema keys do not
  establish player identity. No parser, schema, collector or production state
  was modified to invent a player-ID mapping.

## Decision, not observation

The offline foundation introduces internal IDs keyed by explicit creation-event
UUIDs, independently of names/ranks/photos. It records aliases and provenance,
keeps all name-only resolutions provisional, and quarantines ambiguity.
This is an engineering policy for future canonicalization, not a finding that
the sampled names uniquely identify people. Source-ID support, verified matches,
migration of historical data and manual homonym adjudication remain future work.
