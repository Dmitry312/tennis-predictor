"""Explicit read-only PostgreSQL audit. Never import application modules.

Run only with separately authorized production SSH workflow. Connection settings
are consumed privately from the existing .env; no values/errors are printed.
"""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys

MAX_ROWS = 32
MAX_OUTPUT = 16384
JOBS = ('nbbet-detail', 'nbbet-refresh', 'nbbet-sweep', 'nbbet-sweep-wide', 'health')
STATUSES = ('running', 'ok', 'error')
MATCH_STATUSES = tuple(range(1, 11))
EXPECTED = {
    'matches': {'match_date': 'date', 'created_at': 'timestamp',
                'details_fetched_at': 'timestamp', 'status': 'int2',
                'details_attempts': 'int2', 'duplicate_of': 'uuid', 'level': 'varchar',
                'has_pbp': 'bool', 'pbp_completeness': 'float8',
                'odds_open_p1': 'numeric', 'odds_open_p2': 'numeric'},
    'job_runs': {'job_name': 'varchar', 'status': 'varchar', 'started_at': 'timestamp',
                 'heartbeat_at': 'timestamp', 'finished_at': 'timestamp',
                 'errors_count': 'int4', 'exit_code': 'int2'},
}
# All application SQL is enumerated here. No caller-supplied SQL/identifiers.
QUERIES = {
    'session': '''SELECT current_setting(%s), current_setting(%s),
                         CURRENT_TIMESTAMP AT TIME ZONE %s LIMIT %s''',
    'schema': '''SELECT c.relname, a.attname, t.typname, c.relkind
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
        JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
        WHERE n.nspname = %s AND c.relname = ANY(%s) AND a.attname = ANY(%s)
          AND a.attnum > 0 AND NOT a.attisdropped
          AND t.typnamespace = 'pg_catalog'::pg_catalog.regnamespace LIMIT %s''',
    'matches': '''SELECT COUNT(*), MIN(match_date), MAX(match_date),
        MAX(created_at), MAX(details_fetched_at),
        COUNT(*) FILTER (WHERE created_at >= %s),
        COUNT(*) FILTER (WHERE match_date IS NULL),
        COUNT(*) FILTER (WHERE duplicate_of IS NOT NULL),
        COUNT(*) FILTER (WHERE (odds_open_p1 IS NULL) <> (odds_open_p2 IS NULL)
                         OR odds_open_p1 <= 1 OR odds_open_p2 <= 1),
        COUNT(*) FILTER (WHERE pbp_completeness < 0 OR pbp_completeness > 1),
        COUNT(*) FILTER (WHERE status = ANY(%s) AND details_fetched_at IS NULL
            AND details_attempts < %s AND duplicate_of IS NULL AND level = ANY(%s)),
        COUNT(*) FILTER (WHERE has_pbp IS TRUE AND pbp_completeness IS NULL)
        FROM public.matches LIMIT %s''',
    'match_statuses': '''SELECT CASE WHEN status = ANY(%s) THEN status ELSE 0 END,
        COUNT(*) FROM public.matches GROUP BY 1 ORDER BY 1 LIMIT %s''',
    'jobs': '''SELECT CASE WHEN job_name = ANY(%s) THEN job_name ELSE 'other' END,
        CASE WHEN status = ANY(%s) THEN status ELSE 'other' END,
        COUNT(*), COALESCE(SUM(errors_count), 0), MAX(started_at), MAX(heartbeat_at),
        MAX(finished_at), MAX(heartbeat_at) FILTER (WHERE status = %s),
        COUNT(*) FILTER (WHERE exit_code IS NOT NULL AND exit_code <> 0),
        COUNT(*) FILTER (WHERE errors_count < 0)
        FROM public.job_runs WHERE started_at >= %s OR status = %s
        GROUP BY 1, 2 ORDER BY 1, 2 LIMIT %s''',
}


class AuditFailure(Exception):
    pass


def rows(cursor, name, params, limit):
    cursor.execute(QUERIES[name], params)
    result = cursor.fetchmany(limit + 1)
    if len(result) > limit:
        raise AuditFailure('result_limit')
    return result


def number(value):
    # Do not stringify arbitrary database objects or accept bool/NaN/Decimal.
    if type(value) is not int or not 0 <= value <= 10**30:
        raise AuditFailure('unexpected_result')
    return value


def timestamp(value, date_only=False):
    if value is None:
        return None
    expected = dt.date if date_only else dt.datetime
    if type(value) is not expected or (not date_only and value.tzinfo is not None):
        raise AuditFailure('unexpected_result')
    return value.isoformat() + ('' if date_only else 'Z')


def age(now, value):
    timestamp(value)
    return None if value is None else int((now - value).total_seconds())


def audit(connection):
    with connection.cursor() as cursor:
        session = rows(cursor, 'session', ('transaction_read_only', 'transaction_isolation', 'UTC', 1), 1)
        if len(session) != 1 or session[0][:2] != ('on', 'repeatable read'):
            raise AuditFailure('readonly_not_confirmed')
        now = session[0][2]
        timestamp(now)
        if now is None:
            raise AuditFailure('unexpected_result')
        schema = rows(cursor, 'schema', ('public', list(EXPECTED),
                      sorted({c for table in EXPECTED.values() for c in table}), MAX_ROWS), MAX_ROWS)
        actual = {table: {} for table in EXPECTED}
        for table, column, kind, relation_kind in schema:
            if table not in EXPECTED or relation_kind not in ('r', 'p'):
                raise AuditFailure('schema_mismatch')
            if column in EXPECTED[table]:
                actual[table][column] = kind
        if actual != EXPECTED:
            raise AuditFailure('schema_mismatch')
        cutoff = now - dt.timedelta(days=1)
        match_rows = rows(cursor, 'matches', (cutoff, [3, 9, 10], 5, ['tour', 'challenger'], 1), 1)
        if len(match_rows) != 1 or len(match_rows[0]) != 12:
            raise AuditFailure('unexpected_result')
        m = match_rows[0]
        matches = {'total': number(m[0]), 'min_match_date': timestamp(m[1], True),
                   'max_match_date': timestamp(m[2], True), 'latest_created_utc': timestamp(m[3]),
                   'latest_details_utc': timestamp(m[4])}
        for label, value in zip(('created_24h', 'missing_match_date', 'marked_duplicates',
                                 'invalid_odds_pair', 'invalid_pbp_completeness',
                                 'detail_queue', 'pbp_without_completeness'), m[5:]):
            matches[label] = number(value)
        status_counts = []
        for status, count in rows(cursor, 'match_statuses', (list(MATCH_STATUSES), 11), 11):
            if type(status) is not int or status not in (0, *MATCH_STATUSES):
                raise AuditFailure('unexpected_result')
            status_counts.append({'status': status, 'count': number(count)})
        jobs = []
        for row in rows(cursor, 'jobs', (list(JOBS), list(STATUSES), 'running', cutoff, 'running', 24), 24):
            job, status, count, errors, started, heartbeat, finished, running_hb, exits, negatives = row
            if job not in (*JOBS, 'other') or status not in (*STATUSES, 'other'):
                raise AuditFailure('unexpected_result')
            jobs.append({'job': job, 'status': status, 'runs': number(count),
                         'errors': number(errors), 'latest_started_utc': timestamp(started),
                         'latest_heartbeat_utc': timestamp(heartbeat),
                         'latest_finished_utc': timestamp(finished),
                         'heartbeat_age_seconds': age(now, heartbeat),
                         'running_heartbeat_age_seconds': age(now, running_hb),
                         'nonzero_exit_count': number(exits), 'negative_error_count': number(negatives)})
        return {'audit_utc': timestamp(now), 'window_hours': 24, 'matches': matches,
                'match_statuses': status_counts, 'jobs_recent_or_running': jobs}


def serialize(report):
    # Called only on the explicitly constructed, type-checked report above.
    text = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    if len(text.encode('utf-8')) > MAX_OUTPUT:
        raise AuditFailure('output_limit')
    return text


def main():
    connection = None
    try:
        for module in ('psycopg2', 'dotenv'):
            if importlib.util.find_spec(module) is None:
                raise AuditFailure('dependency_unavailable')
        try:
            import psycopg2
            from dotenv import dotenv_values
        except ImportError:
            raise AuditFailure('dependency_unavailable') from None
        # Parse privately, without shell sourcing, interpolation or app imports.
        settings = dotenv_values('/root/Tennis_pelmen_model/.env', interpolate=False)
        values = {key: os.environ.get(key, settings.get(key)) for key in
                  ('POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB')}
        if any(not isinstance(v, str) or not v or '${' in v for v in values.values()):
            raise AuditFailure('connection_config_unavailable')
        port = int(values['POSTGRES_PORT'])
        if not 1 <= port <= 65535:
            raise AuditFailure('connection_config_unavailable')
        connection = psycopg2.connect(
            host=values['POSTGRES_HOST'], port=port, user=values['POSTGRES_USER'],
            password=values['POSTGRES_PASSWORD'], dbname=values['POSTGRES_DB'],
            connect_timeout=5, application_name='production-data-audit',
            options='-c default_transaction_read_only=on -c statement_timeout=5000 '
                    '-c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000 '
                    '-c search_path=pg_catalog -c timezone=UTC')
        connection.set_session(readonly=True, isolation_level='REPEATABLE READ', autocommit=False)
        result = serialize(audit(connection))
        connection.rollback()  # Never commit, including on success.
        print(result)
        return 0
    except AuditFailure as error:
        # Only locally defined codes; never render database exceptions.
        allowed = {'result_limit', 'unexpected_result', 'readonly_not_confirmed',
                   'schema_mismatch', 'output_limit', 'dependency_unavailable',
                   'connection_config_unavailable'}
        code = error.args[0] if error.args and error.args[0] in allowed else 'audit_failed'
    except Exception as error:
        code = {'42P01': 'table_unavailable', '42703': 'schema_mismatch',
                '57014': 'query_timeout', '55P03': 'lock_timeout'}.get(
                    getattr(error, 'pgcode', None), 'connection_or_query_failed')
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass
    print(json.dumps({'audit_status': code}))
    return 1


if __name__ == '__main__':
    sys.exit(main())
