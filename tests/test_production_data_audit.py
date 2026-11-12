"""Offline only: no PostgreSQL, SSH, secrets or application imports."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'infra/audit/production_data_audit.py'
spec = importlib.util.spec_from_file_location('production_data_audit', SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
NOW = dt.datetime(2026, 11, 12, 12, 0)
SECRET = 'PRIVATE_DB_VALUE_MUST_NOT_APPEAR'


def fixtures():
    return {
        'session': [('on', 'repeatable read', NOW)],
        'schema': [(table, col, kind, 'r') for table, cols in audit.EXPECTED.items()
                   for col, kind in cols.items()],
        'matches': [(10, dt.date(2026, 1, 1), NOW.date(), NOW, NOW, 3, 1, 1, 0, 0, 2, 0)],
        'match_statuses': [(0, 1), (3, 9)],
        'jobs': [('nbbet-detail', 'running', 1, 0, NOW - dt.timedelta(days=2),
                  NOW - dt.timedelta(seconds=60), None, NOW - dt.timedelta(seconds=60), 0, 0)],
    }


class Cursor:
    def __init__(self, data):
        self.data, self.executed = data, []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, query, params):
        assert query in audit.QUERIES.values()
        assert isinstance(params, tuple) and query.count('%s') == len(params)
        name = next(k for k, v in audit.QUERIES.items() if v == query)
        self.executed.append((name, params))
        self.result = self.data[name]
    def fetchmany(self, n):
        assert 1 <= n <= audit.MAX_ROWS + 1
        return self.result[:n]


class Connection:
    def __init__(self, data=None):
        self.cur = Cursor(fixtures() if data is None else data)
        self.rollbacks, self.closed, self.session = 0, False, None
    def cursor(self):
        return self.cur
    def set_session(self, **kwargs):
        assert not self.cur.executed
        self.session = kwargs
    def rollback(self):
        self.rollbacks += 1
    def close(self):
        self.closed = True


class DataAuditTests(unittest.TestCase):
    def test_sql_allowlist_and_parameters(self):
        self.assertEqual(set(audit.QUERIES), {'session', 'schema', 'matches', 'match_statuses', 'jobs'})
        for query in audit.QUERIES.values():
            self.assertTrue(query.lstrip().startswith('SELECT '))
            self.assertNotIn(';', query)
            self.assertNotRegex(query.upper(), r'\b(INSERT|UPDATE|DELETE|COPY|CALL|CREATE|ALTER|DROP)\b')
            self.assertNotRegex(query, r'error_sample|player[12]|tournament_name|nbbet_slug|pbp_s3_path')
        connection = Connection()
        report = audit.audit(connection)
        self.assertEqual([name for name, _ in connection.cur.executed], list(audit.QUERIES))
        self.assertEqual(report['jobs_recent_or_running'][0]['running_heartbeat_age_seconds'], 60)
        self.assertIn('OR status = %s', audit.QUERIES['jobs'])
        self.assertLess(len(audit.serialize(report).encode()), audit.MAX_OUTPUT)

    def test_readonly_and_schema_fail_closed(self):
        for key, value in [('session', [('off', 'repeatable read', NOW)]),
                           ('schema', []),
                           ('schema', [(t, c, k, 'v') for t, c, k, _ in fixtures()['schema']])]:
            data = fixtures()
            data[key] = value
            with self.assertRaises(audit.AuditFailure):
                audit.audit(Connection(data))

    def test_arbitrary_database_strings_rejected(self):
        for position in (0, 1, 4, 5, 6, 7):
            data = fixtures()
            row = list(data['jobs'][0])
            row[position] = SECRET
            data['jobs'] = [tuple(row)]
            with self.assertRaises(audit.AuditFailure):
                audit.audit(Connection(data))
        for value in (SECRET, float('nan'), True, -1):
            with self.assertRaises(audit.AuditFailure):
                audit.number(value)

    def test_row_and_output_bounds(self):
        data = fixtures()
        data['jobs'] *= 25
        with self.assertRaises(audit.AuditFailure):
            audit.audit(Connection(data))
        with self.assertRaises(audit.AuditFailure):
            audit.serialize({'test': 'x' * audit.MAX_OUTPUT})

    def run_main(self, connection=None, failure=None, dependencies=True, config=None):
        conn = connection or Connection()
        def connect(**kwargs):
            self.assertEqual(kwargs['connect_timeout'], 5)
            for setting in ('default_transaction_read_only=on', 'statement_timeout=5000',
                            'lock_timeout=1000', 'search_path=pg_catalog'):
                self.assertIn(setting, kwargs['options'])
            if failure:
                raise failure
            return conn
        driver = types.ModuleType('psycopg2')
        driver.connect = connect
        if config is None:
            config = ('POSTGRES_HOST=127.0.0.1\nPOSTGRES_PORT=5432\nPOSTGRES_USER=test\n'
                      'POSTGRES_PASSWORD=' + SECRET + '\nPOSTGRES_DB=test\n')
        out = io.StringIO()
        with patch.dict(sys.modules, {'psycopg2': driver}), \
             patch.object(Path, 'open', side_effect=lambda *a, **k: io.BytesIO(config.encode())), \
             patch.object(audit.importlib.util, 'find_spec', return_value=object() if dependencies else None), \
             patch.dict(audit.os.environ, {}, clear=True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = audit.main()
        self.assertNotIn(SECRET, out.getvalue())
        return code, json.loads(out.getvalue()), conn

    def test_main_readonly_rollback_and_cleanup(self):
        code, report, conn = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(conn.session, dict(readonly=True, isolation_level='REPEATABLE READ', autocommit=False))
        self.assertGreaterEqual(conn.rollbacks, 1)
        self.assertTrue(conn.closed)
        self.assertEqual(report['matches']['total'], 10)

    def test_safe_failures(self):
        self.assertEqual(self.run_main(dependencies=False)[1], {'audit_status': 'dependency_unavailable'})
        for state, expected in [('42P01', 'table_unavailable'), ('42703', 'schema_mismatch'),
                                ('57014', 'query_timeout'), ('55P03', 'lock_timeout'),
                                ('28000', 'connection_or_query_failed')]:
            error = RuntimeError(SECRET)
            error.pgcode = state
            code, report, _ = self.run_main(failure=error)
            self.assertEqual(code, 1)
            self.assertEqual(report, {'audit_status': expected})

    def test_dependency_import_failure_is_sanitized(self):
        import builtins
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name == 'psycopg2':
                raise ImportError(SECRET)
            if name == 'dotenv':
                raise AssertionError('optional dotenv must never be imported')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', guarded):
            self.assertEqual(self.run_main()[:2],
                             (1, {'audit_status': 'dependency_unavailable'}))
        def without_dotenv(name, *args, **kwargs):
            if name == 'dotenv':
                raise ImportError('optional dependency deliberately unavailable')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', without_dotenv):
            self.assertEqual(self.run_main()[0], 0)

    def test_literal_env_parser(self):
        settings = audit.parse_env("""# Ignored comment
export POSTGRES_HOST = 'localhost'
POSTGRES_PORT=5432 # comment
POSTGRES_USER="test user"
POSTGRES_PASSWORD='literal # $value'
POSTGRES_DB=test
UNRELATED=$(never_execute_this)
""")
        self.assertEqual(settings['POSTGRES_PASSWORD'], 'literal # $value')
        self.assertEqual(set(settings), set(audit.CONNECTION_FIELDS))
        for text in ('POSTGRES_HOST=x\nPOSTGRES_HOST=y', 'POSTGRES_HOST',
                     'POSTGRES_HOST="unterminated', 'POSTGRES_HOST=x y',
                     'POSTGRES_HOST=${OTHER}', 'POSTGRES_HOST="x" trailing',
                     'POSTGRES_HOST="x\\ny"', 'POSTGRES_HOST=x\x00',
                     'POSTGRES_HOST="multi\nline"', 'x' * 65537):
            with self.subTest(text=text), self.assertRaises(audit.AuditFailure):
                audit.parse_env(text)

    def test_missing_fields_and_invalid_ports_are_sanitized(self):
        valid = dict(POSTGRES_HOST='localhost', POSTGRES_PORT='5432', POSTGRES_USER='test',
                     POSTGRES_PASSWORD=SECRET, POSTGRES_DB='test')
        for key in valid:
            config = ''.join(k + '=' + v + '\n' for k, v in valid.items() if k != key)
            self.assertEqual(self.run_main(config=config)[:2],
                             (1, {'audit_status': 'connection_config_unavailable'}))
        for port in ('abc', '0', '65536', '-1', '+5432', '1.5', '５４３２', '9' * 100):
            config = ''.join(k + '=' + (port if k == 'POSTGRES_PORT' else v) + '\n'
                             for k, v in valid.items())
            self.assertEqual(self.run_main(config=config)[:2],
                             (1, {'audit_status': 'connection_config_unavailable'}))

    def test_env_precedence_and_file_errors(self):
        values = dict(POSTGRES_HOST='localhost', POSTGRES_PORT='5432', POSTGRES_USER='test',
                      POSTGRES_PASSWORD=SECRET, POSTGRES_DB='test')
        with patch.dict(audit.os.environ, values, clear=True), \
             patch.object(Path, 'open', side_effect=FileNotFoundError):
            self.assertEqual(audit.connection_values(), (values, 5432))
        with patch.dict(audit.os.environ, {}, clear=True):
            for problem in (FileNotFoundError(), PermissionError()):
                with patch.object(Path, 'open', side_effect=problem), self.assertRaises(audit.AuditFailure):
                    audit.connection_values()
            for raw in (b'\xff', b'x' * 65537):
                with patch.object(Path, 'open', return_value=io.BytesIO(raw)), self.assertRaises(audit.AuditFailure):
                    audit.connection_values()

    def test_heartbeat_dates_and_extremes(self):
        self.assertIsNone(audit.age(NOW, None))
        self.assertEqual(audit.age(NOW, NOW), 0)
        self.assertEqual(audit.age(NOW, NOW + dt.timedelta(seconds=60)), -60)
        self.assertEqual(audit.age(NOW, NOW + dt.timedelta(microseconds=1)), -1)
        self.assertEqual(audit.age(NOW, NOW - audit.MAX_HEARTBEAT_AGE), 3660 * 86400)
        for now, value in ((None, NOW), (SECRET, NOW), (NOW.date(), NOW),
                           (NOW, SECRET), (NOW, NOW.date()),
                           (NOW.replace(tzinfo=dt.timezone.utc), NOW),
                           (NOW, NOW.replace(tzinfo=dt.timezone.utc)),
                           (NOW, dt.datetime.min), (NOW, dt.datetime.max),
                           (NOW, NOW - audit.MAX_HEARTBEAT_AGE - dt.timedelta(microseconds=1))):
            with self.subTest(now=now, value=value), self.assertRaises(audit.AuditFailure):
                audit.age(now, value)
        data = fixtures()
        row = list(data['jobs'][0])
        row[5] = dt.datetime.max
        data['jobs'] = [tuple(row)]
        self.assertEqual(self.run_main(Connection(data))[:2],
                         (1, {'audit_status': 'heartbeat_age_out_of_range'}))
        for now in (None, SECRET, dt.datetime.min, NOW.date()):
            data = fixtures()
            data['session'] = [('on', 'repeatable read', now)]
            self.assertEqual(self.run_main(Connection(data))[:2],
                             (1, {'audit_status': 'unexpected_result'}))

    def test_search_path_independent_relations(self):
        self.assertIn('public.matches', audit.QUERIES['matches'])
        self.assertIn('public.matches', audit.QUERIES['match_statuses'])
        self.assertIn('public.job_runs', audit.QUERIES['jobs'])
        for relation in ('pg_class', 'pg_namespace', 'pg_attribute', 'pg_type'):
            self.assertIn('pg_catalog.' + relation, audit.QUERIES['schema'])
        self.assertIn('::pg_catalog.regnamespace', audit.QUERIES['schema'])
        # All unqualified callable names are built-ins in pg_catalog (SQL syntax
        # FILTER/WHERE/ANY/IN is not a user-defined function).
        builtins = {'current_setting', 'COUNT', 'MIN', 'MAX', 'COALESCE', 'SUM'}
        syntax = {'FILTER', 'WHERE', 'ANY', 'IN'}
        for query in audit.QUERIES.values():
            calls = set(re.findall(r'\b([A-Za-z_]+)\s*\(', query))
            self.assertLessEqual(calls, builtins | syntax)

    def test_failure_after_connect_closes_connection(self):
        data = fixtures()
        data['schema'] = []
        code, report, conn = self.run_main(Connection(data))
        self.assertEqual((code, report), (1, {'audit_status': 'schema_mismatch'}))
        self.assertTrue(conn.closed)
        self.assertGreaterEqual(conn.rollbacks, 1)


if __name__ == '__main__':
    unittest.main()
