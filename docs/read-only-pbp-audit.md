# Read-only pBp audit (prototype)

Branch-only diagnostic. No production deployment, no cron, no Telegram.

## Command

```bash
PYTHONPATH=src .venv/bin/python -m tennis_predictor.ingestion.audit_pbp \
  --days 1 --max-matches 300 --max-files 300 --max-object-mb 10
```

The command selects canonical matches with terminal statuses 3/9/10 in the previous N **complete Moscow calendar days**. It reads only the referenced pBp Parquet objects, with an upper bound on match count, file count, and per-file size. Database transaction is explicitly read-only and has a 10-second SQL statement timeout. It prints JSON; it does not update the database, S3, collectors, or cron.

## Interpretation

- `structurally_ok`: point/game indices and parser-generated game-complete flags are internally consistent. **Not proof of complete coverage.**
- `review`: unresolved point winner, duplicate or missing index, or incomplete game. Incomplete games may be legitimate for retirement or source-specific tiebreak formats.
- `missing`: no path or no matching match ID in referenced Parquet.
- `error`: file access, schema or decoding failed.

The audit **does not yet** compare the pBp with `nbbet_details/sets_json`, audit `nbbet_stats`, or compare the database against the source's full match list. It cannot assert missing matches, missing terminal games, or missing points with contiguous numbering. It does not audit odds.

## Next implementation gates

1. Create a compact manifest keyed by canonical match ID for `nbbet_details` and `nbbet_stats`, with latest `fetched_at` and provenance. Avoid scanning all Parquet objects on each daily run.
2. Compare pBp set/game totals to final score with retirement, standard tiebreak, and super-tiebreak rules; keep ambiguous cases under `review`.
3. Compare source discovery IDs against canonical PostgreSQL matches to detect entirely missed matches.
4. Validate against sampled real production files, then add daily reporting. Do not enable alerts until false positives are characterized.
