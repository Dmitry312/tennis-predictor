# Surface repair — 2026-11-28

Follow-up to [frozen quality register](data-quality-register-2026-11-27.md). Owner approved targeted production repair and supplied execution output: MATCHED 324, ALREADY CORRECT 0, PENDING 324, PLAN clay 48 / hard 276, COMMITTED. Subsequent read-only run: MATCHED 324, ALREADY CORRECT 324, PENDING 0, PLAN {}, DRY RUN ONLY. These are owner-reported server results, not direct connector observations.

| Edition | Matches | Assigned surface |
|---|---:|---|
| Oeiras 2 2023 |47|hard|
| Salzburg 2022 |47|clay|
| Salinas 2023 |47|hard|
| Charleston March 2024 |43|hard|
| Athens 2025 |39|hard|
| Brussels 2026 |38|hard|
| Brussels 2025 |37|hard|
| Ankara 2026 |12|hard|
| Metz 2026 |12|hard|
| Split 2 2021 |1|clay|
| Astana 2022 |1|hard|

Frozen-cohort projected surface completeness 107895/107895; joint proxy 106968/107895 (99.14%), not certified training eligibility or a fresh full production audit. Remaining proxy failures 927. Historical Parquet objects were not rewritten.

ML branch static review: scripts/acquire_match_samples.py selects surface from PostgreSQL matches; canonical_layer/transform.py uses matches metadata for surface; canonical_match.py reads metadata.surface. Existing pinned sample artifacts must not be assumed refreshed. Use newly acquired PostgreSQL metadata for new dataset versions, preserve old immutable manifests, and test end-to-end precedence over stale PBP surface fields during dataset construction. Strict availability rules still apply. No full S3 rescan or bulk rewrite required for this repair. Source URLs and exact repair manifest are not independently archived in this PR; retain the owner backup and repair script. No runtime ML integration test performed.
