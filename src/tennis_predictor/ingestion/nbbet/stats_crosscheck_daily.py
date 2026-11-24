"""Daily reconciliation status for the existing Telegram report."""
import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from tennis_predictor.ingestion.nbbet.stats_crosscheck import TZ, reconcile


def run(state_dir, apply=False):
    day = datetime.now(TZ).date() - timedelta(days=1)
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    target = state / f"{day}.json"
    def publish(payload):
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        tmp.replace(target)
    publish({"status": "running", "apply": apply})
    try:
        result = reconcile(day=day, lookback=3, apply=apply,
                           max_matches=300, pause=1, deadline=time.monotonic()+2400)
    except Exception as exc:
        publish({"status": "error", "error": type(exc).__name__, "apply": apply})
        raise
    publish({"status": "success", "apply": apply, "summary": result})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir", default="/var/lib/tennis-stats-crosscheck")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    run(args.state_dir, args.apply)


if __name__ == "__main__":
    main()
