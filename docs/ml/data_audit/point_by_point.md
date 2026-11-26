# Point by point

CONFIRMED: 1,350 rows across 11 matches; 5 other PBP objects skipped by decode limits and 4 without checked objects. Exact files are linked in coverage. All inspected point_type strings are empty, sampled surface fields NULL. There is no serve-attempt number, shot type, ace/DF marker, setball field or reason-for-point-ending in these rows.

| Field | Observed semantics / evidence | Confidence / restriction |
|---|---|---|
| canonical_match_id | match UUID joins PG; not player ID | CONFIRMED |
| set_num/game_num/point_num | ordered within match/set/game; observed sequences start1 | CONFIRMED numbering; missing terminal rows can leave no ordinal gap |
| server | 1/2 side from game_server_team1; NULL for tie-break | CONFIRMED writer mapping; no TB server recovery |
| point_winner | 1/2 inferred by writer from score progression, not raw winner bit | CONFIRMED provenance; not independent truth |
| server_won_point | server==winner when both known, otherwise NULL | CONFIRMED derived |
| score_server/score_returner | post-observation score oriented to server; NULL if no server | CONFIRMED writer; not pre-point feature |
| is_break_point/is_match_point | copied source flags; whether before/after-point semantics aligns is not independently established | UNKNOWN for pressure metric use |
| is_tiebreak | game flag copied | CONFIRMED; rule/threshold needs edition evidence |
| game_complete | repeated heuristic for every row in game | CONFIRMED implementation; not certified terminal/full game |
| point_type | empty string in observed rows | CONFIRMED; no causal interpretation |
| gender/level | partition context injected by writer | CONFIRMED; no independent player identity |

[Writer build_parquet_frame](../../../src/tennis_predictor/ingestion/nbbet/pbp_frames.py) computes winner from adjacent post-score states, initializes previous score0:0, orients scores to ordinary-game server, discards side scores for TB (server NULL), and repeats completeness flag. [pbp_parsing.is_game_complete](../../../src/tennis_predictor/ingestion/nbbet/pbp_parsing.py) accepts 40:0/15/30 or advantage as complete ordinary game; those states can occur before the winning rally. CONFIRMED example: [Kukushkin last row](../data_catalog/samples/match_00035326-4450-5d09-b060-86a1f6851e63/pbp.json) set2 game6 point3 is 40:0, game_complete=true; three stored points cannot complete an ordinary game. PBP68 vs source service points82 differs by14 games. This is evidence against treating the flag as full rally history.

The offline auditor checks duplicate ordinal keys, point sequence gaps, score/winner consistency where scores retained, server consistency/alternation, per-set game counts versus details, and service-point denominators. Zero detected gaps would not prove full history; missing terminal rally may not be numbered at all. No currently accepted 'match complete' proof. Tiebreak winner counts may support a hypothesis, but discarded TB side scores and unknown server preclude full score/serve reconstruction. A one-point score change check is weaker than proving every rally existed.

Observed completed sets can be compared by game counts (including TB as one game) but game outcomes cannot be certified from last observed point: advantage can be lost. Retired matches have unfinished final sets; never infer winner from leader. PBP-only matches without detail cannot be labeled from last point.

Potential reconstruction: observed point totals/win rates, ordinary-game server/returner counts, partial run summaries with coverage labels. Full BP opportunities require pre-point score and validated terminal reconstruction. Aces, double faults, first/second serves, winners/unforced errors cannot be reconstructed from current fields. Missing terminal points bias all point-rate/run/pressure measures; P2 features remain gated. Cross-source disagreements are retained, not overwritten.
