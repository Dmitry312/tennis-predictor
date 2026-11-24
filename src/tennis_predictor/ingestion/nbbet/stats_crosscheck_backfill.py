"""Sequential historical audit; dry-run unless explicitly applying."""
import argparse
import json
import time
from datetime import date, timedelta
from pathlib import Path

from tennis_predictor.ingestion.nbbet.stats_crosscheck import reconcile


def run(start, end, state, apply=False, max_days=7, max_matches=300, pause=1.0, reverse=False):
    if start > end or not 1 <= max_days <= 30 or pause < 0.5:
        raise ValueError("invalid historical audit bounds")
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    day = end if reverse else start
    step = timedelta(days=-1 if reverse else 1)
    processed = 0
    while start <= day <= end and processed < max_days:
        marker = state / f"{day.isoformat()}.{'apply' if apply else 'audit'}.json"
        if not marker.exists():
            result = reconcile(day=day, lookback=1, apply=apply,
                               max_matches=max_matches, pause=pause,
                               deadline=time.monotonic() + 2400)
            tmp = marker.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, default=str), encoding="utf-8")
            tmp.replace(marker)
            processed += 1
        day += step
    return not start <= day <= end


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True, type=date.fromisoformat)
    p.add_argument("--end", required=True, type=date.fromisoformat)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--max-days", type=int, default=7)
    p.add_argument("--apply", action="store_true")
    p.add_argument('--max-matches', type=int, default=300)
    p.add_argument('--pause', type=float, default=1.0)
    p.add_argument('--reverse', action='store_true')
    args = p.parse_args()
    if not 1 <= args.max_matches <= 5000:
        p.error('invalid match budget')
    print("complete:", run(args.start, args.end, args.state_dir,
                           apply=args.apply, max_days=args.max_days,
                           max_matches=args.max_matches, pause=args.pause, reverse=args.reverse))


if __name__ == "__main__":
    main()
