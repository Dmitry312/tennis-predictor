# ML data inventory and modeling roadmap

Baseline: main after PR #48 (9ce3124), 2026-11-24. This is a code-derived inventory, not a production coverage audit.

## Storage and scope

PostgreSQL match metadata; Yandex S3 Parquet; NBBet source; Timeweb server; GitHub Actions CI; Telegram monitoring. Documented historical range 2018–2026, actual bounds and completeness pending. Men/women; tour/challenger/itf/exhibition; hard/clay/grass. Train on Tour and Challenger only, exclude ITF. Odds are not model inputs; stored odds are used for historical betting validation regardless of capture/backfill timestamp (report timing limitations separately).

## PostgreSQL matches (27 fields)

| Field | Meaning |
|---|---|
| match_id | Canonical UUID |
| nbbet_match_id | Source ID |
| championat_match_id | Alternate source ID |
| nbbet_slug | Source page slug |
| gender | M/F |
| level | tour/challenger/itf/exhibition |
| tournament_name | Tournament name |
| match_date | Match date |
| start_ts | Start timestamp, naive UTC |
| player1 | P1 name |
| player2 | P2 name |
| surface | Surface, nullable |
| status | NBBet status |
| duplicate_of | Canonical record reference; exclude duplicate rows |
| odds_open_p1 | First observed P1 odds |
| odds_open_p2 | First observed P2 odds |
| odds_open_captured_at | Capture time |
| odds_open_source | listing/page |
| odds_open_status | Status at capture |
| has_pbp | PBP presence |
| pbp_s3_path | PBP object pointer |
| pbp_completeness | Fraction of observed games complete, not proof of full match coverage |
| details_fetched_at | Details fetch time |
| details_attempted_at | Last attempt |
| details_attempts | Attempt count |
| details_version | Details schema version |
| created_at | Record creation time |

Status codes: 1 scheduled, 2 live, 3 finished, 4 moved, 5 canceled, 6 stopped, 7 interrupted, 8 walkover, 9 finished retired, 10 awarded. Initial training proposal: status 3; separately evaluate retired/awarded matches. No winner column in ORM. No canonical player entity yet. JobRun is operational telemetry, not a feature source.

## S3 nbbet_details/

Writer fields: canonical_match_id, nbbet_match_id, status, has_coverage, sets_json, extra_json, players_json, p1_rank, p2_rank, p1_rank_tour, p2_rank_tour, details_version. Additional storage metadata may exist. Grain: match/version. sets_json contains set score/duration candidates; verify exact keys, orientation, units and target extraction. extra_json is court/weather candidate, not validated structured data. Rank is documented by parser as match-date ATP/WTA rank; verify pre-match availability. Older schema versions lack ranks.

## S3 nbbet_stats/

Grain: canonical_match_id, scope, version. scope=0 whole match; positive scope=set number. Each base field has _p1 and _p2 suffix (28 values):
- aces
- double_faults
- first_serve_points_all
- first_serve_points_won
- second_serve_points_all
- second_serve_points_won
- break_points_won
- break_points_all
- winners
- unforced_errors
- max_points_in_row
- max_games_in_row
- service_games_won
- service_games_all

Break-point fields were empty in the documented 60-match Tour/Challenger sample; measure actual coverage. Validate denominators, additive counters and missing values. Do not sum match and set scopes. Corrections append versions; select deterministically by fetched_at with tie/conflict handling and snapshot provenance.

## S3 pbp/

Grain: point. All 17 writer columns:

| Field | Meaning |
|---|---|
| canonical_match_id | Join key |
| gender | Gender |
| level | Tournament level |
| surface | Writer currently emits null; use PostgreSQL |
| set_num | Set number |
| game_num | Game number |
| point_num | Point number |
| server | P1/P2 (1/2), nullable |
| point_winner | Reconstructed P1/P2 winner, nullable |
| server_won_point | Nullable boolean |
| score_server | Post-point server score |
| score_returner | Post-point returner score |
| is_break_point | Source flag |
| is_match_point | Source flag |
| is_tiebreak | Tie-break flag |
| point_type | Semantics pending |
| game_complete | Whole-game terminal-state flag |

Tie-break server and server-relative scores currently null. Missing source flags may become false. Validate score progression, completeness, orientation and duplicate points. Reconstruct pre-point score before deriving pressure-state metrics. Do not use current-match game_complete or outcomes as prematch inputs.

## S3 nbbet_raw/

canonical_match_id, nbbet_match_id, raw_json (pageTennisEvent). Raw storage introduced in details version 3; historical availability unknown. Potential additional odds/player/court/weather fields require semantic review.

## Missing or unverified

Age, height, weight, handedness, ranking points, serve speed/direction, rally length, shot type, injuries, tournament geography, indoor/outdoor, structured weather, altitude, court speed, closing odds and odds movement. Player identity resolution is a prerequisite; names alone are not reliable IDs.

## Feature hypotheses (not implemented)

| Family | Candidate features |
|---|---|
| Ratings | ATP/WTA rank, rank difference/trend, overall and surface Elo, opponent-adjusted Elo, Glicko-2 |
| Form | Match/set/game/point win rates, streaks, opponent strength, surface/level form, recency trend |
| Serve/return | First serve in (verify denominator), first/second serve won, service/return points won, first/second return won, ace/DF rates, hold/break rates, BP saved/converted, dominance ratio |
| Pressure | Tie-break and deciding-set win rates, BP/MP performance, hold probability from 0:30, 0:40, 30:30, deuce, comebacks, lost leads, performance after lost set or service game, point streaks |
| Workload | Days since last match, matches/sets/games/points/minutes in windows, consecutive playing days, current tournament workload, recent retirements, surface transitions, inactivity |
| H2H/style | Match/set/game/point H2H, surface H2H, time since last meeting, P1 serve vs P2 return and vice versa |

Candidate windows: last 5/10/20/50/100 matches; 7/14/30/90/180/365 days; overall, surface, level, similar opponent strength and recency-weighted variants. Select empirically, avoid combinatorial explosion. Preserve sample sizes and missingness; shrink sparse estimates. All hypotheses require out-of-time testing and ablation.

## Proposed architecture

1. Canonical data layer: identity resolution, deduplication, validated scores/targets, versioned snapshot.
2. Match aggregation: player-match records from details/stats/PBP.
3. Chronological player state engine: incremental state, overall/surface ratings, rolling aggregates.
4. Feature store: versioned prematch features, provenance and availability timestamps.
5. Training dataset: one row per match, target separate, chronological splits.
6. Model training: baseline Elo/logistic model, then evaluate boosted trees and other candidates; compare log loss, Brier score and calibration.
7. Calibration on separate chronological validation data.
8. Backtesting: stored odds separate from features, margin-aware comparisons, report timing/selection limitations.
9. Production inference: same feature code as historical replay.

No leakage: update player state only after prior match completion is known; start_ts alone is insufficient for same-day matches. If completion unavailable, use conservative date-level ordering. Historical corrections may be used for retrospective clean-data experiments but must not be confused with true as-of snapshots. Record dataset cutoff and correction versions. No random splits. No repeated per-match full-history DB/S3 queries; batch reads, local columnar staging, incremental checkpoints. Version code, datasets, features and models. Preserve nulls rather than replacing with zeros. Use warm-up history before evaluation. run_features.py is currently a stub.

## Historical correction status

PR #48 merged; systemd tennis-stats-history enabled and observed active on server 2026-11-24. First completed date not verified. Worker newest-first, append-only corrections, one day per batch, 2-second request pause, 60-second inter-batch pause, low CPU/IO priority, 25% CPU quota, 08–12 Moscow pause between batches, stop on error. Shared lock only protects cooperating processes. Repeated S3 scans may be expensive. Check progress with:

```sh
find /var/lib/tennis-stats-history -maxdepth 1 -name '*.apply.json' -printf '%f\n' 2>/dev/null | sort | head -1 | cut -d. -f1
```

A marker is not independent proof of data quality; verify summaries and continuous coverage before export. Feature replay runs oldest-first, unlike correction worker.

## Next thread: first tasks

- Quantitative read-only inventory: unique matches by year/gender/level/surface/status; player counts; per-field null rates; PBP coverage and quality; rank/odds coverage; correction frontier; object sizes and schema versions.
- Validate target extraction and player orientation, player identity, completion-time policy.
- Design canonical snapshot and efficient export of a verified recent interval with sufficient warm-up.
- Discuss mathematical modeling and feature contracts before mass generation.
- Autonomous safe development; explicit approval before PR merge, production changes, data deletion or significant costs.

## Source files

- src/tennis_predictor/storage/models.py
- src/tennis_predictor/ingestion/nbbet/details.py
- src/tennis_predictor/ingestion/nbbet/pbp_frames.py
- src/tennis_predictor/ingestion/nbbet/status.py
- src/tennis_predictor/ingestion/nbbet/stats_crosscheck.py
- src/tennis_predictor/ingestion/nbbet/stats_crosscheck_worker.py
- src/tennis_predictor/pipeline/run_features.py
- docs/parquet-schema-audit-v1.md
- docs/manual-parquet-data-dictionary.md
- docs/nbbet-stats-crosscheck.md

This document distinguishes schema evidence, unverified production coverage and proposed modeling decisions. It does not authorize production changes.
