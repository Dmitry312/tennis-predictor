"""Точка входа для пересчёта признаков (Elo, форма, статистика серва/ретёрна).

TODO: читать очищенные данные из S3, обновлять агрегаты, писать признаки
обратно в S3 (features-слой) для последующего инференса.
"""

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("run_features: пока не реализовано")


if __name__ == "__main__":
    main()
