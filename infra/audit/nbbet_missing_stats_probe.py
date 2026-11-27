"""Compatibility entrypoint for PR #102.
The former live/source-payload probe is replaced by read-only partition coverage.
No HTTP requests to NBBet and no raw-page/statistic payload output.
"""
from production_stats_coverage import main

if __name__=="__main__": raise SystemExit(main())
