# Статус миграции данных

Живой трекер переноса исходных SQLite-баз (`tennis_prediction_model_old/`) в новую схему (Postgres `Match` + партиционированный Parquet на S3). Обновляется по мере прогресса — не журнал решений (см. `decisions.md`), а снимок текущего состояния.

Обновлено: 2026-10-05

## Сделано
- nb-bet point-by-point (`matches_main`/`games_stats`/`points_stats`) →
  Postgres `Match` + S3 `pbp/gender=.../level=.../year=.../`
  (`backfill_historical_pbp.py`, 78,207 матчей)
- S3-бакет очищен от дублей сырых бэкапов (13 → 2 копии)
- Регулярный сбор nb-bet (tour/challenger, без ITF) запущен: `sweep.py`
  закрыл весь исторический разрыв (`status`/`nbbet_slug` у 172,413
  матчей, полный обход 2016-01-01..сегодня), `detail_job.py` на cron
  (`*/10 * * * *`) догружает страницу/статистику/очки для матчей со
  статусом FINISHED/FINISHED_RETIRED/AWARDED без `details_fetched_at`.
  Пилот на 300 реальных матчах: `has_pbp`=100% и у коротких (2:0), и у
  матчей с решающим сетом — старое смещение `score1>0 AND score2>0`
  было ограничением только sqlite-сборщика, не живого API.

## Не начато
- championat.db (`tournaments`, `matches`, `match_stats`,
  `match_timeline`, `tournament_prizes`, `atp_rankings`, `wta_rankings`)
  → S3/Postgres

## Открытые вопросы
- Код расходится с образом: `sweep.py`/`detail_job.py` и сегодняшние
  изменения `Match` работают только из venv на хосте — ничего не
  закоммичено/запушено, образ `ghcr.io/.../tennis-predictor:latest`
  их не содержит, контейнер `app` сейчас не поднят вовсе (только
  `postgres`). Решение (отдельный сервис в compose с монтированием
  кода, либо пересборка образа) нужно принять до того, как в cron
  добавятся другие джобы (рейтинги, турниры) — иначе у каждой будет
  свой случайный способ запуска.
- Схема хранения под championat: отдельные S3-префиксы (`championat/...`)
  по аналогии с `pbp/`, или расширение `Match` (там уже есть
  `championat_match_id`) + отдельные Postgres-таблицы?
- Таблица `job_runs` для регулярного коллектора (обсуждалась, не
  создана) — нужна, когда `sweep`+`detail_job` заменят разовый прогон
  на обычный цикл «sweep за последние дни + detail_job по новым».
