# Стадия сборки: используем официальный образ uv для быстрой установки зависимостей
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Сначала только манифесты — слой с зависимостями кэшируется отдельно от кода
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

# Финальный образ — без uv и без билд-инструментов, только venv и код
FROM python:3.12-slim-bookworm

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/src ./src

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "tennis_predictor.pipeline.run_inference"]
