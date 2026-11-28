# Rating eligibility on the 20-match catalog

This audit reproduced the historical replay smoke test used in PR #101.
It built Canonical Layer and Match Data Store from the checked-in catalog
using the test's locally approved fixture registry. Both player IDs were
resolved for each match. No row was excluded because of identity, date, or
deduplication. The IDs below are NBBet match IDs; set scores and winning
sides follow the source side order.

| NBBet ID | UTC date | Rating replay | Evidence |
| --- | --- | --- | --- |
| 81415 | 2016-08-02 | Excluded | Finished (3). No set score or canonical winner. |
| 94721 | 2017-02-08 | Excluded | Finished (3). No set score or canonical winner. |
| 26041 | 2017-02-27 | Excluded | Finished (3). No set score or canonical winner. |
| 207964 | 2018-03-22 | Included | Finished (3). Side 1 won 2–0 from 6–3, 6–4. |
| 135037 | 2019-09-24 | Excluded | Finished retired (9). Play ended at 1–6, 2–4. No canonical winner. |
| 249197 | 2020-01-15 | Excluded | Finished (3). No set score or canonical winner. |
| 318197 | 2021-05-31 | Included | Finished (3). Side 2 won 2–0 from 0–6, 5–7. |
| 335934 | 2021-08-27 | Excluded | Finished (3). No set score or canonical winner. |
| 368682 | 2022-03-14 | Included | Finished (3). Side 1 won 2–0 from 6–2, 6–0. |
| 453599 | 2023-04-21 | Excluded | Walkover (8). No played result or canonical winner. |
| 461686 | 2023-05-30 | Excluded | Finished retired (9). Play ended at 6–3, 2–1. No canonical winner. |
| 531103 | 2024-03-26 | Included | Finished (3). Side 1 won 2–0 from 6–4, 7–6. |
| 571806 | 2024-08-22 | Excluded | Scheduled (1). No completed result. |
| 577961 | 2024-09-14 | Excluded | Finished (3). No set score or canonical winner. |
| 605600 | 2025-02-02 | Excluded | Canceled (5). Match Data Store emitted no win facts. |
| 691300 | 2026-02-10 | Excluded | Finished retired (9). No set score or canonical winner. |
| 719212 | 2026-06-02 | Included | Finished (3). Side 1 won 2–0 from 7–6, 6–2. |
| 725766 | 2026-06-22 | Excluded | Finished (3). No set score or canonical winner. |
| 730766 | 2026-07-08 | Included | Finished (3). Side 2 won 2–1 from 6–7, 6–3, 3–6. |
| 750496 | 2026-09-18 | Included | Finished (3). Side 2 won 2–0 from 4–6, 2–6. |

Seven matches supplied complementary valid win facts and updated the
ratings. Of the 13 exclusions, seven had finished status with no validated
score and six lacked a completed result eligible for rating under the
existing status contract. The fixture checks integration on the catalog.
It does not measure coverage of the full registry or historical database.
