"""Long-running newest-first worker. Explicit --apply required for S3 writes."""
import argparse
import fcntl
import logging
import time
from datetime import datetime, timedelta
from sqlalchemy import func, select

from tennis_predictor.ingestion.nbbet.stats_crosscheck import TZ
from tennis_predictor.ingestion.nbbet.stats_crosscheck_backfill import run
from tennis_predictor.storage.db import engine
from tennis_predictor.storage.models import Match

log = logging.getLogger(__name__)


def bounds():
    with engine.connect() as conn:
        start = conn.execute(select(func.min(Match.match_date)).where(
            Match.status.in_((3, 9, 10)), Match.details_fetched_at.is_not(None),
            Match.nbbet_slug.is_not(None), Match.duplicate_of.is_(None))).scalar()
    return start, datetime.now(TZ).date() - timedelta(days=1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--state-dir", default="/var/lib/tennis-stats-history")
    p.add_argument("--lock", default="/tmp/nbbet_stats_crosscheck.lock")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    start, end = bounds()
    if start is None or start > end:
        return
    while True:
        # Leave the morning collection/report window free.
        if 8 <= datetime.now(TZ).hour < 12:
            time.sleep(300)
            continue
        with open(args.lock, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                try:
                    if run(start, end, args.state_dir, apply=args.apply,
                           max_days=1, max_matches=1500, pause=2.0, reverse=True):
                        return
                except Exception:
                    log.exception("Historical batch failed; stopping for investigation")
                    raise
        time.sleep(60)


if __name__ == "__main__":
    main()
