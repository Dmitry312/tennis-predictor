# Statistics semantics and reconciliation

CONFIRMED provenance: [details.py STAT_FIELDS/parse_match_page](../../../src/tennis_predictor/ingestion/nbbet/details.py) maps raw match["25"][scope][code] pairs to metric_p1/p2 without converting percentages. Scope0 = match; scope1.. = set ordinal. All 25 rows (8 matches) were inspected; values are integer counts or NULL, not percent strings. Source labels/units are INFERRED from code plus count identities unless independently verified. Preserve original and confidence.

| Code | Metric | Unit / aggregation | Interpretation and limitations |
|---:|---|---|---|
| 1 | aces | count; sum disjoint complete scopes only | Ace count; point_type currently empty, cannot verify shot outcome |
| 2 | double_faults | count; sum disjoint complete scopes only | Double fault count; no first/second-serve flag in sampled PBP |
| 3 | first_serve_points_all | count; sum disjoint complete scopes only | First-serve point count; won/all form a ratio, not a stored percent |
| 4 | first_serve_points_won | count; sum disjoint complete scopes only | First-serve point count; won/all form a ratio, not a stored percent |
| 5 | second_serve_points_all | count; sum disjoint complete scopes only | Second-serve point count; won/all form a ratio |
| 6 | second_serve_points_won | count; sum disjoint complete scopes only | Second-serve point count; won/all form a ratio |
| 7 | break_points_won | count; sum disjoint complete scopes only | Break chances converted/opportunities by player; NULL differs from zero |
| 8 | break_points_all | count; sum disjoint complete scopes only | Break chances converted/opportunities by player; NULL differs from zero |
| 9 | winners | count; sum disjoint complete scopes only | Shot outcome count; not independently recoverable from current PBP |
| 10 | unforced_errors | count; sum disjoint complete scopes only | Shot outcome count; not independently recoverable from current PBP |
| 11 | max_points_in_row | maximum; NONADDITIVE | Consecutive run maximum; do not sum set maxima (runs may span boundaries) |
| 12 | max_games_in_row | maximum; NONADDITIVE | Consecutive run maximum; do not sum set maxima (runs may span boundaries) |
| 13 | service_games_won | count; sum disjoint complete scopes only | Service games won/played; exclude TB ambiguity before comparison |
| 14 | service_games_all | count; sum disjoint complete scopes only | Service games won/played; exclude TB ambiguity before comparison |

CONFIRMED: metric suffix belongs to player side, maintained from raw pair order; no score-based swapping. Numeric counters must be >=0 integers; won<=all when paired. NULL is unknown/not supplied, never zero. Zero is an explicit observed source value (e.g. Kukushkin/Soeda double_faults). No percentage averaging: sum won and all over eligible prior observations, divide once, report denominator and coverage. Ace/DF rates need compatible service-point denominators; verify whether faults are included before claiming a serve-attempt rate.

All available additive match totals equal sums of observed set rows (auditor reports every comparison). This is a within-snapshot consistency check, not universal proof of additive scope semantics. Winners/unforced errors exist only for Noskova and Juvan (6 scope rows); maxima are only present at match scope in five matches. Break-point fields are present in 15 scope rows and absent in other real snapshots, contradicting the old code comment 'always empty on tour/challenger'. Code behavior is unchanged; this audit supersedes that factual assumption for this sample.

| Evidence | PBP rows | First+second service-point totals, both players | Observed games | Assessment |
|---|---:|---:|---:|---|
| [00014491 stats](../data_catalog/samples/match_00014491-b904-5816-bd67-b8d6226351d7/nbbet_stats.json) / [PBP](../data_catalog/samples/match_00014491-b904-5816-bd67-b8d6226351d7/pbp.json) | 63 | 78 | 12 | CONFLICT: denominators differ |
| [00025c54 stats](../data_catalog/samples/match_00025c54-c0ac-50cf-ab44-c344c5131e3c/nbbet_stats.json) / [PBP](../data_catalog/samples/match_00025c54-c0ac-50cf-ab44-c344c5131e3c/pbp.json) | 106 | 124 | 18 | CONFLICT: denominators differ |
| [0002f543 stats](../data_catalog/samples/match_0002f543-0bb9-52e8-b3ca-a2d6d62fdb2f/nbbet_stats.json) / [PBP](../data_catalog/samples/match_0002f543-0bb9-52e8-b3ca-a2d6d62fdb2f/pbp.json) | 201 | 232 | 31 | CONFLICT: denominators differ |
| [00035326 stats](../data_catalog/samples/match_00035326-4450-5d09-b060-86a1f6851e63/nbbet_stats.json) / [PBP](../data_catalog/samples/match_00035326-4450-5d09-b060-86a1f6851e63/pbp.json) | 68 | 82 | 14 | CONFLICT: denominators differ |
| [00039e3e stats](../data_catalog/samples/match_00039e3e-7b1f-5ec6-bc8b-d95ee2661c83/nbbet_stats.json) / [PBP](../data_catalog/samples/match_00039e3e-7b1f-5ec6-bc8b-d95ee2661c83/pbp.json) | 105 | 120 | 18 | CONFLICT: denominators differ |
| [0004963d stats](../data_catalog/samples/match_0004963d-f247-5440-8e2f-176585d6ccb9/nbbet_stats.json) / [PBP](../data_catalog/samples/match_0004963d-f247-5440-8e2f-176585d6ccb9/pbp.json) | 151 | 174 | 23 | CONFLICT: denominators differ |
| [00070fb7 stats](../data_catalog/samples/match_00070fb7-fcbb-5cdc-ab62-8a72bcf61c1a/nbbet_stats.json) / [PBP](../data_catalog/samples/match_00070fb7-fcbb-5cdc-ab62-8a72bcf61c1a/pbp.json) | 102 | 121 | 19 | CONFLICT: denominators differ |
| [001ed393 stats](../data_catalog/samples/match_001ed393-59a1-56db-a9f4-bcf0f5d5fa73/nbbet_stats.json) / [PBP](../data_catalog/samples/match_001ed393-59a1-56db-a9f4-bcf0f5d5fa73/pbp.json) | 111 | 132 | 21 | CONFLICT: denominators differ |

INFERRED: several differences equal one omitted terminal point per game; not all do (Noskova 15 vs12 games, Stoiana15 vs18). Do not repair by adding a point per game. Thus exact ace/DF, serve win, BP and service-game reconciliation via PBP cannot be certified. [stats_crosscheck.py](../../../src/tennis_predictor/ingestion/nbbet/stats_crosscheck.py) append-only versions may exist outside checked siblings; the catalog is not the latest complete statistics inventory. Duplicate scope or correction conflicts must be retained and quarantined, not summed.
