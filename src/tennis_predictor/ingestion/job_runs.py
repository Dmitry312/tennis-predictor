"""Журнал запусков: одна строка на запуск, heartbeat по ходу работы.
Сбой записи в журнал не должен ронять саму джобу."""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, update

from tennis_predictor.storage.db import engine
from tennis_predictor.storage.models import JobRun

logger = logging.getLogger(__name__)
STALE_AFTER = timedelta(minutes=60)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class RunHandle:
    def __init__(self, run_id: int | None):
        self.run_id = run_id
        self.inserted = self.updated = self.skipped = self.errors = 0
        self.error_sample: str | None = None
        self.exit_code = 0

    def add(self, *, inserted=0, updated=0, skipped=0, errors=0, error_sample=None) -> None:
        self.inserted += inserted
        self.updated += updated
        self.skipped += skipped
        self.errors += errors
        if error_sample and not self.error_sample:
            self.error_sample = error_sample[:500]
        self._write(heartbeat_at=_now())

    def _write(self, **extra) -> None:
        if self.run_id is None:
            return
        try:
            with engine.begin() as conn:
                conn.execute(update(JobRun).where(JobRun.id == self.run_id).values(
                    inserted=self.inserted, updated=self.updated, skipped=self.skipped,
                    errors_count=self.errors, error_sample=self.error_sample, **extra))
        except Exception:
            logger.warning("job_runs: запись не удалась", exc_info=True)


@contextmanager
def track_run(job_name: str):
    now = _now()
    run_id = None
    try:
        with engine.begin() as conn:
            # оборванные прошлые запуски этой джобы закрываем как ошибку
            conn.execute(update(JobRun).where(
                JobRun.job_name == job_name, JobRun.status == "running",
                JobRun.heartbeat_at < now - STALE_AFTER,
            ).values(status="error", finished_at=JobRun.heartbeat_at,
                     error_sample="процесс оборвался без завершения"))
            run_id = conn.execute(insert(JobRun).values(
                job_name=job_name, started_at=now, heartbeat_at=now, status="running",
            ).returning(JobRun.id)).scalar_one()
    except Exception:
        logger.warning("job_runs: не удалось завести запись", exc_info=True)

    h = RunHandle(run_id)
    try:
        yield h
    except BaseException as e:
        h.exit_code = 1
        h.error_sample = h.error_sample or repr(e)[:500]
        h._write(status="error", finished_at=_now(), heartbeat_at=_now(), exit_code=1)
        raise
    h._write(status="ok" if h.exit_code == 0 else "error",
             finished_at=_now(), heartbeat_at=_now(), exit_code=h.exit_code)
