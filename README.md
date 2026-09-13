# tennis-predictor

Движок прогнозирования и детекции value-bets на ATP/WTA одиночном разряде.
Единый слой базовых вероятностей (бустинг) → марковская модель → согласованные
вероятности по всем прематч-рынкам (виннер, тоталы, форы).

## Структура

```
src/tennis_predictor/
├── ingestion/     # парсеры матчей, статистики, коэффициентов
├── storage/       # клиенты S3 и Postgres
├── features/      # Elo, форма, серв/ретёрн статистика
├── models/
│   ├── boosting/  # обучение и инференс градиентного бустинга
│   └── markov/    # closed-form / симуляция гейм → сет → матч
├── pricing/       # расчёт рынков и детекция value-bets
├── backtest/       # бэктест-движок
├── pipeline/      # точки входа, которые дёргает cron
└── config.py      # настройки (pydantic-settings)

migrations/        # Alembic
infra/docker/      # Dockerfile и docker-compose
.github/workflows/ # CI/CD: сборка образа в GHCR + деплой на сервер
```

## Быстрый старт (локально)

```bash
cp .env.example .env          # заполнить своими значениями
uv sync                       # поставит зависимости и создаст .venv

docker compose -f infra/docker/docker-compose.local.yml up -d   # Postgres для разработки
uv run alembic -c migrations/alembic.ini upgrade head

uv run pytest
```

## Деплой

Пуш в `main` триггерит `.github/workflows/deploy.yml`: образ собирается в
GitHub Actions, пушится в GHCR, затем по SSH на сервере выполняется
`docker compose pull && docker compose up -d`. Секреты (`SERVER_HOST`,
`SERVER_USER`, `SERVER_SSH_KEY`) настраиваются в Settings → Secrets репозитория.

На сервере ожидается путь `/opt/tennis-predictor` с файлом `.env` и
`infra/docker/docker-compose.yml` (см. workflow).
