# Статус миграции данных

Живой трекер переноса исходных SQLite-баз (`tennis_prediction_model_old/`) в новую схему (Postgres `Match` + партиционированный Parquet на S3). Обновляется по мере прогресса — не журнал решений (см. `decisions.md`), а снимок текущего состояния.

Обновлено: 2026-09-16

## Сделано
- nb-bet point-by-point (`matches_main`/`games_stats`/`points_stats`) →
  Postgres `Match` + S3 `pbp/gender=.../level=.../year=.../`
  (`backfill_historical_pbp.py`, 78,207 матчей)
- S3-бакет очищен от дублей сырых бэкапов (13 → 2 копии)

## Не начато
- championat.db (`tournaments`, `matches`, `match_stats`,
  `match_timeline`, `tournament_prizes`, `atp_rankings`, `wta_rankings`)
  → S3/Postgres

## Открытые вопросы
- Нужно ли заносить в `Match` ~425K matches nb-bet без pbp
  (есть счёт, но не было точек)?
- Схема хранения под championat: отдельные S3-префиксы (`championat/...`)
  по аналогии с `pbp/`, или расширение `Match` (там уже есть
  `championat_match_id`) + отдельные Postgres-таблицы?
