# Parquet content audit (2026)

The inventory audit only checks S3 metadata. This second stage reads **projected columns** from Parquet for one year and reconciles IDs between datasets.

Run without modifying the checkout:

```bash
cd /root/Tennis_pelmen_model
git fetch origin feature/read-only-data-quality-audit
git show origin/feature/read-only-data-quality-audit:src/tennis_predictor/ingestion/audit_s3_content.py > /tmp/tennis_audit_s3_content.py
PYTHONPATH=src .venv/bin/python /tmp/tennis_audit_s3_content.py --year 2026 --max-files 2000 --max-object-mb 5 > /tmp/tennis_s3_audit_2026.json
```

The command reads S3 only. It does not change production code, DB, cron, or S3. The output JSON contains counts and small samples of match IDs, not raw page contents or credentials. It may take several minutes depending on S3 throughput.

**Interpretation:** Missing stats or pBp does not necessarily mean corruption; the upstream source may lack coverage. File counts are not match counts. Multiple files can contain the same match; unique match counts deduplicate across files, but row counts do not. A match appearing in multiple historical versions is not automatically an error. Inspect `skipped_size` and `unreadable_objects` before drawing conclusions: if nonzero, counts may be incomplete.

The module does not compare to PostgreSQL or upstream discovery, nor validate final set scores. Those remain separate gates.
