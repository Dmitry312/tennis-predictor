# Availability and historical replay

These classifications are conservative proposals, not observed availability guarantees. Categories: 1 proven prematch; 2 postmatch; 3 eligible for future matches after state update; 4 unknown time; 5 forbidden features; 6 needs evidence. A field can be postmatch AND future-eligible (2,3).

| Field family | Categories | Safe use |
|---|---|---|
| event date/start, players, gender/level, surface, tournament/round | 4,6 (normally prematch in concept) | category1 only with independently stored available_at<=cutoff |
| score, winner, status final, duration, match/set stats, PBP | 2,3; 5 for current match | update future state after result availability only |
| ranking observation | 4,6 | need effective date AND publication/availability version; not fetched historical rank automatically |
| odds incl raw price blocks | 5 | never model features, separate backtest timestamps |
| source match ID, UUID, photo, ingestion/version/path | 5 as predictive features | joins/provenance only; avoid ID/time/selection shortcuts |
| has_pbp, completeness, has_coverage, details attempts/version | 5 | coverage gates, not player-strength proxies |
| weather/venue/flags, raw unused fields/popularMatches/prose | 4,6; 5 until approved | no broad raw JSON feature extraction |
| future-updated player state/Elo/form | 3,6 | only state snapshot as known before prediction |

Define event_time (sporting occurrence), available_at (first defensibly observed accessible version), ingested_at (our receipt), prediction_cutoff (decision instant), source_version/content_hash (immutable payload), effective_from/effective_to (meaning validity), recorded_at (system history). They must not collapse into one timestamp. Missing timezone/time precision is explicit. NULL means unknown, never 'always available'.

Strict historical mode: no field without evidenced available_at<=cutoff; result updates require result availability earlier than next cutoff; same-day unknown order excluded. Retrospective observations fetched2026 cannot be treated as known in2016. No global dataset identity registry snapshot from the future: pin journal revision and recorded_at gate in [canonical_match.prematch_context](../../../src/tennis_predictor/ml/canonical_match.py).

Alternative research-only event-order reconstruction may use past event outcomes with conservative end-of-day+embargo ordering, but must label availability assumed, exclude overlapping/same-day ambiguous matches, and report sensitivity. Embargo cannot prove absent publication timestamps. Keep this dataset separate from strict as-of/backtest metrics; do not advertise it as leakage-free reproduction. Rebuild state in chronological order with frozen training/validation boundaries, no future corrections retroactively inserted. Missing rank snapshots excluded; missing stats retained with coverage denominator rather than zero imputation. No training in this PR.

CONFIRMED example of future page context: Noskova2023 raw popularMatches contains October2026 dates; see [context evidence](context_and_rankings.md). Exclude the entire recommendation subtree, not merely its timestamps. Full raw_json is provenance, never an unrestricted feature input.
