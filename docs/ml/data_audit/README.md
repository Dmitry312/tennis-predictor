# Real catalog semantic audit

Snapshot: `500ff871a2c8de19af558170ebe83cabca766d4a` (post PR #86). This audit concerns these 20 selected matches, not the historical population. All inputs are committed GitHub files. No production, PostgreSQL, S3, external API, new acquisition, ETL or model training.

Evidence statuses: **CONFIRMED** = directly observed or traced transformation; **INFERRED** = plausible interpretation requiring more evidence; **UNKNOWN** = insufficient evidence; **CONFLICT** = inconsistent observations, not an automatic correction.

One command, from any working directory: `python /path/to/repository/scripts/audit_ml_catalog.py > report.json` (normally `python scripts/audit_ml_catalog.py > report.json`). Standard library only, reads repository JSON, writes only stdout. Output is deterministic for the same files. Findings do not fail the command: malformed input fails; known incompleteness is reported. Tests enforce counts, hashes, determinism and conflict detection.

CONFIRMED: 20 matches rows; 9 details rows; 25 statistics scope rows; 1,350 PBP rows; 5 raw snapshots. Eleven matches have PBP records, nine details, eight statistics, five raw. No stable player ID is established; all 40 PostgreSQL player names are distinct, so cross-match/cross-year identity cannot be tested here. Numeric keys are schema labels, not IDs.

CONFIRMED: sampled sets_json uses descriptive keys (score_team1/score_team2/duration/tiebreak_score_team1/tiebreak_score_team2), not the unknown numeric structures in synthetic review documentation. Five raw snapshots preserve the same score arrays. CONFIRMED: old dictionary/allowlist describe a separate synthetic/sanitized projection and must not determine real-source score semantics. No canonicalization rule is changed here.

CONFLICT: PBP rows and statistics service-point totals differ in all eight jointly observed matches. Often the difference equals the number of games; that suggests omitted terminal points, not permission to synthesize them. The point writer labels some ordinary games complete from 40-x or advantage states, which do not prove the final rally exists. Full match history remains unverified.

Read [coverage](coverage.md), [identity](player_identity.md), [results](match_results.md), [statistics](match_statistics.md), [PBP](point_by_point.md), [context](context_and_rankings.md), [time/leakage](data_leakage.md), [proposed contracts](canonical_contracts.md), [features](feature_catalog.md), [open questions](open_questions.md). The contracts are proposals for review, not an implemented ETL or permission to train.
