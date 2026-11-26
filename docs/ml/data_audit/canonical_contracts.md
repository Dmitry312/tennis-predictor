# Proposed canonical contracts from the completed audit

The [Canonical Data Layer v1 guide](../canonical-layer.md) documents the implemented physical schemas and how they differ from this audit proposal. Types below are logical SQL/Arrow types. Every entity is versioned: add the common fields below to EACH row in all 11 schemas. FK targets and composite keys are explicit; source-side orientation is never silently swapped. proposal = design, not a factual discovery. Source paths refer to the [coverage matrix](coverage.md), [loader](../../../src/tennis_predictor/ingestion/nbbet/details.py), [model](../../../src/tennis_predictor/storage/models.py) and [existing contracts](../canonical-match.md).

Common fields for every entity (same columns apply individually):

| Field | Type | Source / transformation | Required | NULL | Unit | Key/time | ML | Confidence / limit |
|---|---|---|---|---|---|---|---|---|
| manifest_id | text FK data_manifest | immutable source+transform reference | yes | no | hash | version binding | audit only | proposal; immutable |
| event_time | timestamptz | sport event; never use ingestion as event | no | yes | UTC | event time | gated | UNKNOWN precision/time zone requires evidence |
| available_at | timestamptz | evidenced first accessible version | no | yes | UTC | as-of predicate | required gate | UNKNOWN in historical samples |
| ingested_at | timestamptz | source fetched_at/PG receipt with provenance | no | yes | UTC | system receipt | not feature | CONFIRMED observed receipt, not first publication |
| recorded_at | timestamptz | canonical decision time | yes | no | UTC | append-only system history | registry gate | proposal |
| source_version | text | hash + source version + schema | yes | no | version | composite PK version | no | proposal |
| evidence_status | enum | CONFIRMED/INFERRED/UNKNOWN/CONFLICT | yes | no | category | audit version | gate | proposal |
| evidence_ref | text | exact file/row/JSON pointer | yes | no | reference | lineage | no | proposal |
| transform_version | text | mapping/rules/identity revision digest | yes | no | version | reproducibility | no | proposal |

## players

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| player_id | uuid | internal create-event UUID5 | immutable surrogate PK | yes | no | identity | N/A | join only | proposal |
| gender | enum M/F | PG matches.gender + adjudication | never infer from name | yes | no | category | validity interval | context gate | CONFIRMED source |
| display_name | text | PG/player objects | preserve original, no identity guarantee | yes | no | text | observed_at | no direct feature | CONFIRMED |
| resolution_status | enum provisional/verified/quarantine | registry evidence | name-only always provisional | yes | no | status | recorded_at | eligibility gate | proposal |

## player_aliases

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| alias_id | uuid | registry event | append-only alias/create ID PK | yes | no | identity | recorded_at | join only | proposal |
| player_id | uuid FK players | registry decision | explicit linkage only | yes | no | identity | validity interval | join only | proposal |
| source_name | text | PG/player JSON field1 | preserve spelling | yes | no | text | observed_at | no | CONFIRMED |
| normalized_name | text | source_name | NFC/lower/whitespace version; no transliteration | yes | no | text | normalization version | join candidate | proposal |
| source_player_id | text | none proven | NULL until independent source-ID evidence | no | yes | identity | validity interval | join only | UNKNOWN |
| evidence_ref | text | sample path/side/record | exact JSON pointer plus hash | yes | no | reference | observed_at | audit only | proposal |

## tournaments

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| tournament_id | uuid | curated registry | internal PK; not name hash alone | yes | no | identity | validity interval | join | proposal |
| edition_id | uuid | curated edition | edition/date/rules key unique | yes | no | identity | event interval | context | proposal |
| name_raw | text | PG tournament_name/raw match.1.1 | preserve | yes | no | text | available_at unknown | gated | CONFIRMED |
| level | enum | PG level | tour/challenger; retain source classifier version | no | yes | category | available_at | gated | CONFIRMED |
| best_of | int 3/5 | edition rule evidence | never infer solely from score/gender | no | yes | sets | effective interval | target gate | UNKNOWN |
| decider_rule | enum | edition rule evidence | TB7/TB10/advantage/matchTB10 | no | yes | rule | effective interval | target gate | UNKNOWN |

## matches

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| match_id | uuid | PG match_id | retain source mapping; canonical duplicate relation explicit PK | yes | no | identity | event_time | join | CONFIRMED |
| source_match_id | int | nbbet_match_id | source namespace required, not player ID | no | yes | identity | version | join | CONFIRMED |
| player1_id | uuid FK players | identity registry | preserve source side1 | no | yes | identity | registry revision | gate | proposal |
| player2_id | uuid FK players | identity registry | distinct from player1, preserve side2 | no | yes | identity | registry revision | gate | proposal |
| edition_id | uuid FK tournaments | curated mapping | quarantine ambiguous tournament | no | yes | identity | available_at | gated | proposal |
| start_time | timestamptz | PG start_ts/raw match.2 | UTC only with documented source convention | no | yes | UTC instant | event_time; revision possible | gated | INFERRED |
| surface | enum hard/clay/grass | PG surface | retain unknown, parser provenance | no | yes | category | available_at unknown | gated | CONFIRMED |
| duplicate_of | uuid FK matches | PG duplicate_of + review | no destructive delete | no | yes | identity | recorded_at | exclude duplicates | CONFIRMED source |

## match_results

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| match_id | uuid FK matches | source joined result | PK with source_version | yes | no | identity | result event_time | label join | proposal |
| status | int | PG/details/raw.23 | preserve enum; disagree quarantine | no | yes | status | result available_at | not current features | CONFIRMED |
| sets | json | details sets_json/raw.24 | typed ordered games/TB/duration plus original | no | yes | games/points/raw duration | postmatch | past-state only | CONFIRMED shape |
| winner_side | int 1/2 | score rules + corroboration | NULL if retirement/format/conflict unresolved | no | yes | side | result available_at | label | proposal |
| target_p1_win | int 0/1 | winner_side | only approved terminal/identity/time policy | no | yes | binary | result available_at | label only | proposal |
| exclusion_reason | text enum | validation | required if target NULL | no | yes | reason | audit version | quality only | proposal |

## match_player_stats

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| match_id | uuid FK matches | canonical_match_id | PK with player_side/scope/source_version | yes | no | identity | event_time | past-state join | CONFIRMED |
| player_side | int 1/2 | metric suffix | unpivot pairs, preserve orientation | yes | no | side | match version | join | CONFIRMED |
| scope | int >=0 | scope | 0 match, 1+ set; never sum match+sets | yes | no | scope | event period | past-state | CONFIRMED |
| aces | int >=0 | nbbet_stats.aces_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| double_faults | int >=0 | nbbet_stats.double_faults_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| first_serve_points_all | int >=0 | nbbet_stats.first_serve_points_all_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| first_serve_points_won | int >=0 | nbbet_stats.first_serve_points_won_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| second_serve_points_all | int >=0 | nbbet_stats.second_serve_points_all_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| second_serve_points_won | int >=0 | nbbet_stats.second_serve_points_won_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| break_points_won | int >=0 | nbbet_stats.break_points_won_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| break_points_all | int >=0 | nbbet_stats.break_points_all_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| winners | int >=0 | nbbet_stats.winners_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| unforced_errors | int >=0 | nbbet_stats.unforced_errors_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| max_points_in_row | int >=0 | nbbet_stats.max_points_in_row_p{side} | preserve NULL; nonadditive maximum | no | yes | points/count | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| max_games_in_row | int >=0 | nbbet_stats.max_games_in_row_p{side} | preserve NULL; nonadditive maximum | no | yes | games | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| service_games_won | int >=0 | nbbet_stats.service_games_won_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | games | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |
| service_games_all | int >=0 | nbbet_stats.service_games_all_p{side} | preserve NULL; sum only compatible complete disjoint scopes | no | yes | games | postmatch available_at | prior-state with coverage | INFERRED units; CONFIRMED mapping |

## match_player_pbp

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| match_id | uuid FK matches | canonical_match_id | PK with set/game/point/source_version | yes | no | identity | event_time | past-state join | CONFIRMED |
| set_num | int >=1 | PBP set_num | preserve | yes | no | ordinal | event order | past-state | CONFIRMED |
| game_num | int >=1 | PBP game_num | within set | yes | no | ordinal | event order | past-state | CONFIRMED |
| point_num | int >=1 | PBP point_num | within game; gaps explicit | yes | no | ordinal | event order | past-state | CONFIRMED |
| server_side | int 1/2 | PBP server | NULL TB; do not invent rotation | no | yes | side | point event | gated prior-state | CONFIRMED |
| winner_side | int 1/2 | PBP point_winner | derived source observation not independent truth | no | yes | side | postpoint | gated prior-state | CONFIRMED provenance |
| score_server | text | PBP score_server | postpoint score, preserve A/AD | no | yes | score | postpoint | not current feature | CONFIRMED |
| score_returner | text | PBP score_returner | postpoint score, NULL TB | no | yes | score | postpoint | not current feature | CONFIRMED |
| pressure_flags | json | is_break_point/is_match_point | retain raw flags; timing unknown | no | yes | boolean map | UNKNOWN | not until validated | UNKNOWN |
| is_tiebreak | bool | PBP is_tiebreak | retain flag | yes | no | boolean | game event | gated | CONFIRMED |
| game_complete_source | bool | PBP game_complete | keep separately from independently verified completeness | no | yes | boolean | ingested_at | quality only | CONFIRMED heuristic |
| point_type | text | PBP point_type | empty string not serve/shot classification | no | yes | category unknown | point event | not until evidenced | UNKNOWN |

## player_rankings

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| player_id | uuid FK players | resolved rank side | PK with tour/effective_at/source_version | yes | no | identity | registry revision | join | proposal |
| tour | text ATP/WTA | p1/2_rank_tour | not ranking points | no | yes | category | effective_at unknown | gated | CONFIRMED label |
| rank | int >0 | p1/2_rank | preserve observed number | no | yes | position | effective_at UNKNOWN | not without as-of proof | CONFIRMED value |
| effective_at | timestamptz | not present | NULL; do not fill match date automatically | no | yes | UTC | ranking validity | gate | UNKNOWN |

## match_odds

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| match_id | uuid FK matches | PG match_id | PK with source_version/captured_at | yes | no | identity | event_time | backtest join | CONFIRMED |
| price_p1 | decimal(7,3) | odds_open_p1 | pair finite >1 <=500 | no | yes | decimal odds | captured_at | FORBIDDEN feature | CONFIRMED |
| price_p2 | decimal(7,3) | odds_open_p2 | pair NULL together | no | yes | decimal odds | captured_at | FORBIDDEN feature | CONFIRMED |
| captured_at | timestamptz | odds_open_captured_at | UTC convention; not market publication time | no | yes | UTC | our observation | backtest gate | CONFIRMED |
| source_status | int | odds_open_status | all observed3; no prematch claim | no | yes | status | captured_at | FORBIDDEN feature | CONFIRMED |
| bookmaker | text | not evidenced | NULL | no | yes | identity | quote time unknown | backtest only | UNKNOWN |

## data_quality

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| quality_id | text | hash(entity/rule/version/evidence) | PK stable rule observation | yes | no | identity | audit version | quality only | proposal |
| entity_ref | text | table/key/field/source row | FK-like exact typed reference | yes | no | reference | source version | quality only | proposal |
| status | enum CONFIRMED/INFERRED/UNKNOWN/CONFLICT | audit evidence | do not conflate severity with confidence | yes | no | status | observed_at | gate only | proposal |
| coverage_state | enum | export version coverage | missing/path-unchecked/decode-skipped/partial/link-unknown/selected | yes | no | state | source version | coverage denominator | CONFIRMED source |
| reason | text enum | auditor/validator | fixed reason, preserve observations | yes | no | reason | audit version | quality only | proposal |

## data_manifest

| Field | Type | Source | Transformation / keys | Required | NULL | Unit | Temporal semantics | ML use | Evidence / limitations |
|---|---|---|---|---|---|---|---|---|---|
| manifest_id | text sha256 | immutable input/output hashes | PK deterministic content version | yes | no | hash | version | reproducibility | proposal |
| source_ref | text | path/record/field/object key | exact provenance, not network read | yes | no | reference | source version | audit only | CONFIRMED |
| content_sha256 | text hex64 | manifest/files/content hash | verify before parsing semantics | yes | no | hash | version | audit only | CONFIRMED |
| source_version | text | etag/version_id/details_version/schema | retain all separately; no freshness inference | no | yes | version | observed_at | audit only | CONFIRMED |
| selection_plan | text sha256 | acquisition plan hash | selected population not all history | no | yes | hash | acquisition | audit only | CONFIRMED |
| operation_ref | text | acquisition-review receipt | batch/operation and bounded coverage | no | yes | reference | export time | audit only | CONFIRMED |

## Conflict, correction and partial-record policy

Preserve source observations in immutable versioned staging. Validate hashes first. Do not collapse raw and details into independent confirmations: details is derived from raw family. Disagreeing status/score/side/metric rows produce data_quality CONFLICT; target NULL until adjudicated, no arbitrary source priority. An explicit reviewed result reference may resolve a conflict with new version, not overwrite old evidence. Append corrections with effective and recorded time; replay uses only versions available at cutoff. Same-version duplicate identical content deduplicates; divergent same-key content quarantines. Source IDs and duplicate_of are evidence for review, not sufficient to merge real-world matches by themselves.

If side order differs, require an explicit bijection through resolved players and swap all score/stat/odds sides together under a new transform version; otherwise quarantine. Matches with unresolved identity, format, terminal status, availability or source conflicts remain queryable but ineligible for strict training. Partial data never becomes zero/full history; include group/row coverage, unknown denominators and missing-reason enums. Derived features must declare their denominator, minimum coverage and rule version. Sports NULL attributes do not prevent source manifest preservation. No verified player/ranking/target is fabricated to satisfy NOT NULL: required unresolved keys stay outside canonical training views.
