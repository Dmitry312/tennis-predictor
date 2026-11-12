# Manual production data audit (proposal; not executed)

Only `production-data-audit.yml`, manually dispatched from main, may stream the
standalone script over verified ED25519 SSH. It never imports application code,
health.py, models, engine or track_run. Existing workflows are unchanged.
No deployment or production query is authorized by this document.

## Connection and transaction

The existing `/root/Tennis_pelmen_model/.venv/bin/python` is used with `-I -B`:
no cwd/project imports, no Python bytecode writes. Only psycopg2 is
required and checked at runtime without installation; a missing/broken driver fails closed.
Actual production dependency availability has NOT been probed during preparation.
The existing `.env` is parsed privately by a bounded stdlib parser (no dotenv,
source, eval or interpolation). Only the
five POSTGRES connection fields are consumed, with matching environment variables
taking precedence. All five must be explicit, including POSTGRES_PORT. Missing
values or ${...} interpolation stop the audit; no guessed credentials or defaults.
The file is limited to 64 KiB and each value to 4096 characters. Supported syntax:
optional export, KEY=value, single/double quoted literal values, and trailing #
comments. Duplicate connection keys, escapes, multiline values, NUL, malformed
quotes and ambiguous unquoted values fail closed. Unrelated keys are ignored.
The file may be absent only when all five fields exist in the environment. Ports
must contain 1..5 ASCII digits with numeric value 1..65535; no fallback is used.
No DSN, credentials or exception messages are printed. No Docker command is used.

libpq startup options enforce default_transaction_read_only=on,
statement_timeout=5000ms, lock_timeout=1000ms, idle_in_transaction_session_timeout=10000ms,
search_path=pg_catalog and timezone=UTC. connect_timeout is 5 seconds. The driver
starts a REPEATABLE READ, READ ONLY transaction; the first SELECT confirms these
properties before application tables are touched. There is no commit: rollback
and close occur on success and failure. Session/transaction controls are necessary
protocol setup; all enumerated audit queries themselves are parameterized SELECT.
A dedicated read-only database role would add defence in depth, but is not created
or required by this PR. No database configuration is changed.

## Exhaustive query allowlist

`infra/audit/production_data_audit.py:QUERIES` is the only source of SQL.
No workflow input, shell expansion or caller-provided SQL/identifier is accepted.

| Query | Result and limit |
| --- | --- |
| session | Read-only/isolation validation and DB UTC timestamp; 1 row |
| schema | Only required columns/types of public.matches and public.job_runs; max 32 rows, 18 expected; plain/partitioned tables, built-in types only |
| matches | Total count, min/max match dates, latest creation/details dates, new rows in 24h, missing dates, marked duplicates, invalid/missing odds pair, out-of-range PBP completeness, pending detail queue, PBP with missing completeness; 1 row |
| match_statuses | Counts in fixed numeric buckets 1..10; 0 combines NULL/unrecognised statuses; max 11 rows |
| jobs | Counts/errors/nonzero exits, latest timestamps and heartbeat ages, grouped only by five known jobs plus other and running/ok/error/other; max 24 rows |

The jobs query includes runs started in the last 24 hours **or still marked
running**, even when started earlier. Thus job counts/error totals include those
older running rows; they are not pure 24-hour totals. Negative heartbeat ages
expose future timestamps rather than silently clamping them. Types of both the
reference clock and heartbeat must be naive datetime. Integer arithmetic avoids
float rounding; even a 1-microsecond future heartbeat reports -1 second (floor).
Absolute ages over 3660 days fail with heartbeat_age_out_of_range; this is a
sanity bound, not a freshness threshold. Missing heartbeat remains null. Empty jobs results
mean no matching journal entries, not proof that cron is healthy. Marked duplicate
counts are not a full duplicate detection algorithm. Match dates are event dates,
not ingestion timestamps; latest_created/latest_details describe different work.
The pending queue mirrors health.py's statuses 3/9/10, attempts<5, no duplicate,
and tour/challenger levels; it does not run collectors or prove data completeness.

Schema checks happen before aggregates. Missing tables, columns, incompatible
types or views stop the audit. No schema discovery output is printed. Each result
is fetched with a bound; only known status labels, integers and typed dates are
serialized. error_sample/player/tournament/URL/DSN fields are never selected.
One success JSON line is capped at 16384 UTF-8 bytes; failures emit only a fixed
local error code. Driver/remote stderr is suppressed to avoid accidental leakage.

## Limits, tests and risks

Job timeout 4 minutes, keyscan 15 seconds, SSH connection timeout 15 seconds,
whole SSH invocation 60 seconds (+2-second kill grace); no parallel audit runs.
Heavy counts can scan whole tables and incur read I/O/cache/CPU load; READ ONLY
still takes access-share locks. Short query/lock timeouts bound this impact but
cannot promise zero production interference. Timeout means incomplete audit,
not permission to increase limits automatically. No SQL function that performs
application writes is called. Physical PostgreSQL statistics/logs, SSH audit,
OS atime and interpreter startup hooks may still cause incidental system writes.

Offline tests use fake DB cursors/connections: SQL allowlist, bound parameters,
row limits, malicious database strings, dates/numbers, serialization budget,
read-only setup, rollback/cleanup, schema mismatch and sanitized error codes.
Additional local verification executed all five parameterized queries (PREPARE /
EXECUTE) against disposable PostgreSQL 16 with search_path=pg_catalog, public fixture
tables and a READ ONLY REPEATABLE READ transaction. Explicit public table names
resolve independently of search_path; catalog relations and regnamespace are
qualified and unqualified functions/operators are PostgreSQL built-ins. The local
container had no network, exposed ports or persistent volume and was removed.
These checks do not prove production credentials, installed dependencies, data,
plans or load; no production PostgreSQL connection was made.

After separate review/merge authorization, verify the production deploy remains
blocked. Any manual audit dispatch requires a separate explicit authorization.
Do not modify existing collectors, cron, Docker, migrations or secrets as a result
of this report without a separate task. Do not upload raw diagnostic output as an
artifact; only the bounded aggregate log is intended for review.
