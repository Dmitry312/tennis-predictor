"""Точка входа для дневного инференса: считаем прогнозы на сегодняшнюю линию.

TODO: загрузить модель бустинга из S3, посчитать point-win probability,
прогнать марковскую модель, сравнить с линией букмекеров, записать
value-bets в Postgres.
"""

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("run_inference: пока не реализовано")


if __name__ == "__main__":
    main()
