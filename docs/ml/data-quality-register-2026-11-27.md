# ML data quality register — 2026-11-27 snapshot

Approved policy: [statistics coverage decision](stats-coverage-decision-2026-11-27.md).
Owner supplied read-only SQLite checkpoint output; PG snapshot 2026-11-27T14:47:53.338037Z. Stable completed stats/details inventory. No new production reads or writes for this document.

## Final filtered coverage

112928 eligible status-3 matches from 2021, minus 5033 Davis Cup/BJK Cup/Asian Games matches = 107895. All 15 excluded tournament names in supplied output match intended exclusions (including two doubles-labelled records). Regex alias coverage is not independently exhaustive.

Columns: N = denominator, S = recognized surface, D = nonconflicting expected set count present, P = PG PBP flag, U = usable scope0 stats, C = all 16 core scope0 fields nonnull, J = S AND D AND P AND C. Stats latest-version conflicts excluded. D is not verified score or winner; P is not physical PBP completeness; C is not value correctness or full set statistics. J is a readiness proxy, not certified training eligibility.

| Group | N | S | D | P | U | C | J |
|---|---:|---:|---:|---:|---:|---:|---:|
| All |107895|107571|107890|107731|107104|107057|106644|
| 2021 |13807|13806|13807|13759|13113|13111|13101|
| 2022 |16918|16870|16917|16898|16897|16895|16835|
| 2023 |18080|17986|18080|18070|18074|18046|17943|
| 2024 |18868|18825|18868|18846|18850|18843|18783|
| 2025 |18744|18668|18744|18713|18725|18720|18620|
| 2026 |21478|21416|21474|21445|21445|21442|21362|
| ATP Tour |23228|23113|23227|23219|23056|23030|22910|
| WTA Tour |22476|22476|22476|22455|22287|22284|22266|
| ATP Challenger |54110|53944|54108|54005|53704|53686|53470|
| WTA Challenger |8081|8038|8079|8052|8057|8057|7998|
| Hard |55469|55469|55466|55379|55162|55153|55110|
| Clay |44758|44758|44756|44699|44568|44560|44525|
| Grass |7344|7344|7344|7329|7050|7020|7009|
| Unknown |324|0|324|324|324|324|0|

Usable stats 99.27%, 791 missing; core 99.22%, 838 missing; joint 98.84%, 1251 fail. These exact filtered figures supersede the earlier estimated missing count. 2022–2026 joint 93543/94088 = 99.42%. All 324 unknown surfaces pass other proxy criteria; resolving them would raise joint to 106968/107895 = 99.14%, conditional on reliable mapping.

## Register and next steps

- History and tournament exclusions: decided; implement feature-specific filters in ML.
- Original statistics coverage: audited and bulk recovery closed; no zero imputation.
- PBP metadata coverage: audited; 191933/199068 flags on Nov 12, 107731/107895 on current filtered snapshot. [Nov 12 audit](https://github.com/Dmitry312/tennis-predictor/actions/runs/43261448717). Physical existence sampled 24/24 on [Nov 16](https://github.com/Dmitry312/tennis-predictor/actions/runs/43883586782); no exhaustive content validation. Do not repeat metadata audit without reason.
- Surface: 324 unknown in frozen snapshot; compare prior backfill results and current state before proposing targeted repair. No repair authorized here.
- Details: five lack known expected set count; actual winner/score validity not established by cache.
- Duplicate/date/player metadata: previous SQL audit; use canonical filters and validate labels/identity during dataset construction.
- Parquet schema: documented in ../parquet-schema-audit-v1.md; schema is not semantic validation.
- Odds: excluded from features; historical betting evaluation policy unchanged.
- Leakage, actual result validity, PBP semantics and feature-specific eligibility: validate during dataset construction. Do not claim J as final training-ready count.

No additional full S3 scan required for this report. Retain all source data. No production changes.
