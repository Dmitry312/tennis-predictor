# ML data eligibility and statistics coverage — decision 2026-11-27

Owner-approved decision: close bulk recovery of missing original match statistics for the current ML phase.

## Binding modeling policy

- Working history starts 2021-01-01. Preserve older source data; no deletion or historical recovery before 2021.
- ATP/WTA Tour and Challenger singles, canonical completed matches (status 3) for initial training. Exclude Davis Cup, Billie Jean King Cup (including Fed Cup aliases), and Asian Games. Validate tournament aliases; do not exclude ordinary qualifying rounds.
- Exclude matches lacking required statistics from statistics-dependent training datasets. Do not impute missing statistics as zero or fabricate first/second serve splits, aces or double faults from untyped PBP.
- Retain otherwise eligible matches with reliable results for Elo even if extended statistics are absent. Document feature-specific eligibility and chronological availability; no future leakage.
- Do not undertake bulk reconstruction of missing original statistics from PBP or mass refetch for this gap. Preserve raw/PBP/statistics. Ongoing collection and correction of existing statistics are separate tasks.
- Audit PBP, surface, score and other required metadata separately. No claim of full ML readiness based on statistics presence alone.

## Production audit snapshot

PG snapshot 2026-11-27T14:47:53.338037Z; completed second inventory pass, 9721 objects (4986 details, 4735 statistics), 873024 rows, stable inventory, no unknown coverage category. Audit implementation PR #102, commit ee430dbf5adf81c6039cd9b83b9a1eea1eee66fe. Read-only audit; source value correctness not established. Results supplied by owner from server report.

| Period/status | Matches | Usable scope 0 | Absent | Full core |
|---|---:|---:|---:|---:|
| 2016–2026 status 3 | 166654 | 124973 | 41681 | 124791 |
| 2016–2026 status 9 | 5381 | 4055 | 1326 | 3892 |
| 2016–2020 status 3 | 53726 | 16717 | 37009 | — |
| 2021–2026 status 3 | 112928 | 108256 | 4672 | — |

### Working history before tournament exclusions

| Year | Matches | Usable scope 0 | Absent | Coverage |
|---|---:|---:|---:|---:|
| 2021 | 14359 | 13264 | 1095 | 92.37% |
| 2022 | 17819 | 17112 | 707 | 96.03% |
| 2023 | 19352 | 18290 | 1062 | 94.51% |
| 2024 | 20041 | 19035 | 1006 | 94.98% |
| 2025 | 19457 | 18939 | 518 | 97.34% |
| 2026 | 21900 | 21616 | 284 | 98.70% |
| Total | 112928 | 108256 | 4672 | 95.86% |

| Category | Matches | Usable scope 0 | Absent |
|---|---:|---:|---:|
| WTA Challenger | 8081 | 8057 | 24 |
| WTA Tour | 24859 | 22735 | 2124 |
| ATP Challenger | 54110 | 53704 | 406 |
| ATP Tour | 25878 | 23760 | 2118 |

### After tournament exclusions

Top-20 missing counts identify at least 1904 Davis Cup and 1893 Billie Jean King Cup missing matches; Asian Games adds 67 (34 women, 33 men, all missing). Thus at most 808 missing matches remain after these exclusions, or 0.716% of the original 112928 denominator. Exact filtered denominator and exact filtered coverage were not supplied; do not report 99.284% as filtered coverage. Approximate residual gap is around 1%, not a verified exact rate.

Largest remaining gaps: Wimbledon women 137 (all 2021), men 124 (123 in 2021, 1 in 2026); Biella 47, Antalya Challenger 43, Quimper 42, Istanbul 40, Porto 34, St Petersburg 25. Most of these concentrated in 2021.

## Source investigation

Ten controls (NBBet IDs 323421, 323581, 323683, 323684, 726136, 301639, 298750, 299740, 284772, 324435): all status 3, coverage flag true, sets present, raw match["25"] empty list, parsed scopes zero. All ten had saved PBP with point winners and empty point_type throughout. This establishes current source absence for these controls, not all missing matches or all historical raw versions. PBP game completeness and terminal-point representation not independently validated; no parser defect established.

## Follow-up

Use this decision in modeling threads instead of reopening bulk recovery. Exact filtered coverage can be computed from existing audit checkpoint during next readiness audit; no need to rescan S3 solely for this. This document does not implement filters, change production, or certify ML readiness.
