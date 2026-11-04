"""add odds_open columns to matches

Revision ID: 439ea87ea48e
Revises: f3c8e3485a04
Create Date: 2026-10-08 19:11:34.153287

"""
import logging
import time

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import OperationalError

# revision identifiers, used by Alembic.
revision = '439ea87ea48e'
down_revision = 'f3c8e3485a04'
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)

# Миграция применяется при работающем detail_job (он держит короткие
# транзакции с UPDATE отдельных строк matches). ADD COLUMN/CHECK — это
# ACCESS EXCLUSIVE на всю таблицу, в обычном режиме мог бы ждать
# неограниченно долго за конкурентной транзакцией. lock_timeout='5s' не
# даёт зависнуть; каждая DDL-команда — в своём autocommit-блоке (а не в
# общей транзакции миграции), чтобы таймаут одной не отравлял остальные
# и можно было повторить именно её.
RETRY_ATTEMPTS = 6
RETRY_DELAY = 10  # секунд между попытками — пауза даёт detail_job закончить батч


def _run_with_retry(sql: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text("SET lock_timeout = '5s'"))
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                op.execute(sa.text(sql))
                return
            except OperationalError as e:
                if attempt == RETRY_ATTEMPTS:
                    raise
                logger.warning(
                    "lock_timeout на попытке %d/%d (%s), жду %dс и повторяю: %s",
                    attempt, RETRY_ATTEMPTS, sql[:60], RETRY_DELAY, e,
                )
                time.sleep(RETRY_DELAY)


def upgrade() -> None:
    _run_with_retry("ALTER TABLE matches ADD COLUMN odds_open_p1 NUMERIC(7,3)")
    _run_with_retry("ALTER TABLE matches ADD COLUMN odds_open_p2 NUMERIC(7,3)")
    _run_with_retry("ALTER TABLE matches ADD COLUMN odds_open_captured_at TIMESTAMP")
    _run_with_retry("ALTER TABLE matches ADD COLUMN odds_open_source VARCHAR(10)")
    _run_with_retry("ALTER TABLE matches ADD COLUMN odds_open_status SMALLINT")
    _run_with_retry(
        "ALTER TABLE matches ADD CONSTRAINT ck_matches_odds_open_gt1 "
        "CHECK (odds_open_p1 > 1 AND odds_open_p2 > 1)"
    )
    _run_with_retry(
        "ALTER TABLE matches ADD CONSTRAINT ck_matches_odds_open_pair "
        "CHECK ((odds_open_p1 IS NULL) = (odds_open_p2 IS NULL))"
    )


def downgrade() -> None:
    _run_with_retry("ALTER TABLE matches DROP CONSTRAINT ck_matches_odds_open_pair")
    _run_with_retry("ALTER TABLE matches DROP CONSTRAINT ck_matches_odds_open_gt1")
    _run_with_retry("ALTER TABLE matches DROP COLUMN odds_open_status")
    _run_with_retry("ALTER TABLE matches DROP COLUMN odds_open_source")
    _run_with_retry("ALTER TABLE matches DROP COLUMN odds_open_captured_at")
    _run_with_retry("ALTER TABLE matches DROP COLUMN odds_open_p2")
    _run_with_retry("ALTER TABLE matches DROP COLUMN odds_open_p1")
