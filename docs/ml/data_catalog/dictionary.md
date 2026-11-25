# Linked review data dictionary v1

Confidence labels: WRITER_DOCUMENTED describes code, not population correctness;
UNVERIFIED marks semantic hypotheses; SYNTHETIC is fabricated fixture data.
Availability is unknown unless established separately; no ingestion timestamp is
silently equated to pre-match availability. Outcome fields are not model features.

| Source/field | Storage / example | Meaning and evidence | Confidence | Availability / leakage |
| --- | --- | --- | --- | --- |
| matches.match_id | UUID, private join only | Match ORM primary key; never a player ID | WRITER_DOCUMENTED | unknown; identifier excluded |
| matches.gender/level | string enums | Match ORM and tournament classification | WRITER_DOCUMENTED | pre-match provenance unknown |
| matches.status | smallint, synthetic 3/9/8 | status.py: finished/retired/walkover | WRITER_DOCUMENTED | post-event outcome; high leakage |
| matches.surface | nullable string enum | Only PostgreSQL field is authoritative per row-review policy | WRITER_DOCUMENTED | timing unknown; do not use pbp.surface |
| matches.duplicate_of | nullable UUID, private filter | Exclude non-null duplicate records | WRITER_DOCUMENTED | correction timing unknown |
| matches.pbp_s3_path | private string | Exact PBP object candidate; sibling current part paths are candidates only | WRITER_DOCUMENTED | may be stale; never publish |
| details.canonical_match_id | private string UUID | Match/version join, not file grain | WRITER_DOCUMENTED | not a feature |
| details.status/details_version | int/null | writer status and schema revision | WRITER_DOCUMENTED | post-event/correction metadata |
| details.has_coverage | bool/null | source coverage marker | UNVERIFIED | not proof of PBP completeness |
| details.sets_json | string/null, projected numeric structures | details.py preserves match["24"]; exact numeric-key meanings not established | UNVERIFIED | final scores/outcome; high leakage |
| details.players_json | string/null/absent, alias-only | v2 adds p1/p2 source objects; no stable ID established | WRITER_DOCUMENTED | no profile values exported |
| stats.scope | integer | 0 whole match, positive set per writer | WRITER_DOCUMENTED | post-event; avoid double counts |
| stats.*_p1 / *_p2 | numeric/null, synthetic 3 / 1.5 / null | 14 STAT_FIELDS pairs in details.py | WRITER_DOCUMENTED | post-event; not prematch feature values |
| pbp.set_num/game_num/point_num | integer | ordered point grain per pbp_frames writer | WRITER_DOCUMENTED | current-match outcome leakage |
| pbp.server/point_winner | side 1/2/null | reconstructed point winner / game server; not match winner | WRITER_DOCUMENTED | orientation needs review |
| pbp.score_server/score_returner | score string/null | source point-score progression | UNVERIFIED | preserve strings; do not infer target |
| pbp.game_complete and flags | bool/null | parser-derived completion/source markers | WRITER_DOCUMENTED | not proof of full match coverage |
| raw.raw_json | string/null/absent | v3 preserves full pageTennisEvent; only safe match projection exported | WRITER_DOCUMENTED | full payload never publishable |
| raw.match["23"] | status int/null | parse_match_page status mapping | WRITER_DOCUMENTED | outcome leakage |
| raw.match["24"] | set list | same unknown score structure as details.sets_json | UNVERIFIED | numeric paths need manual semantic review |
| raw.match["3"]/["5"] | role aliases only | source player objects; name/photo/rank redacted | WRITER_DOCUMENTED | roles are not verified identities |
| independent winner | unavailable by default | ORM has no winner; no reviewed independent source supplied | UNVERIFIED | label evidence only, never a feature |

Sources: storage/models.py; ingestion/nbbet/details.py:STAT_FIELDS/details_to_rows;
detail_job.py:flush; pbp_frames.py; status.py; docs/manual-parquet-data-dictionary.md;
production_parquet_row_review.py; full_match_export.py.
Legacy v1 details may lack players_json; raw introduced in v3; all-null columns and
missing objects must remain explicit. Preserve actual projected primitive type
categories; never treat missing schema fields as zeros.

## Open questions and required review
1. Map specific numeric sets_json keys to P1/P2 games, tiebreak points and duration
   using linked examples plus independent evidence; anonymous K-labels are insufficient.
2. Validate BO3/BO5, deciding-set variants, final vs partial scores and orientation.
3. Establish independent winner reference and conflict policy; do not invent a key.
4. Confirm source timestamps/availability and current-vs-legacy correction selection.
5. Establish real shared-object coverage: same suffix is not proof of existence.
6. Validate sport-value privacy/reidentification risk before public release.
7. Determine representative selection and omitted large-file/row coverage. Current
   exact-input review is deliberately not a population audit.

Synthetic examples in this directory answer none of the production semantic questions.
