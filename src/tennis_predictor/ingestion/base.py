"""Общий интерфейс коллекторов — единый формат результата для cron-логов
вместо разного print() в каждом скрипте."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CollectorResult:
    source: str
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def log_summary(self) -> None:
        logger.info(
            "%s: inserted=%d updated=%d skipped=%d errors=%d",
            self.source, self.inserted, self.updated, self.skipped, len(self.errors),
        )
        for e in self.errors:
            logger.error("%s: %s", self.source, e)


class BaseCollector(ABC):
    name: str

    @abstractmethod
    def run(self, *, dry_run: bool = False) -> CollectorResult: ...
