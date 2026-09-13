"""Точка входа, которую будет дёргать cron для обновления сырых данных.

TODO: подключить существующие парсеры (перенести их логику в
tennis_predictor.ingestion) и писать результат в S3 (сырой слой) с
манифест-файлом, подтверждающим успешное завершение шага.
"""

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("run_ingestion: пока не реализовано")


if __name__ == "__main__":
    main()
