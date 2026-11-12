"""Offline only: fake S3/DB, no SDK, network or production credentials."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'infra/audit/production_s3_audit.py'
spec = importlib.util.spec_from_file_location('production_s3_audit', SCRIPT)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
NOW = dt.datetime(2026, 11, 12, 12, tzinfo=dt.timezone.utc)
SECRET = 'PRIVATE_OBJECT_OR_EXCEPTION_MUST_NOT_APPEAR'
BUCKET = 'test-bucket'


def page(prefix, truncated=False, token=None):
    result = {'KeyCount': 1, 'IsTruncated': truncated, 'Contents': [
        {'Key': prefix + SECRET + '.parquet', 'Size': 7, 'LastModified': NOW}]}
    if token is not None:
        result['NextContinuationToken'] = token
    return result


def empty():
    return {'KeyCount': 0, 'IsTruncated': False}


class S3Error(Exception):
    def __init__(self, status):
        super().__init__(SECRET)
        self.response = {'ResponseMetadata': {'HTTPStatusCode': status}}


class Events:
    def __init__(self):
        self.handlers = {}
    def register(self, name, handler):
        self.handlers[name] = handler


class Client:
    def __init__(self, pages=None, heads=None):
        self.meta = types.SimpleNamespace(events=Events())
        self.pages, self.heads, self.calls, self.closed = pages or {}, heads or {}, [], False
    def before(self, operation, args):
        self.meta.events.handlers['before-call.s3'](model=types.SimpleNamespace(name=operation))
        self.meta.events.handlers['before-send.s3']()
        self.calls.append((operation, args))
    def list_objects_v2(self, **args):
        self.before('ListObjectsV2', args)
        responses = self.pages.get(args['Prefix'], [])
        result = responses.pop(0) if responses else empty()
        if isinstance(result, Exception):
            raise result
        return result
    def head_object(self, **args):
        self.before('HeadObject', args)
        result = self.heads.get(args['Key'], {'ResponseMetadata': {'HTTPStatusCode': 200},
                                            'ContentLength': 7, 'LastModified': NOW})
        if isinstance(result, Exception):
            raise result
        return result
    def close(self):
        self.closed = True


def database_data():
    return {'session': [('on', 'repeatable read', NOW.replace(tzinfo=None))],
            'schema': [('varchar', 'r')], 'counts': [(5, 3, 2, 1, 0)],
            'sample': [('pbp/' + SECRET + '.parquet',), ('pbp/second.parquet',)]}


class Cursor:
    def __init__(self, data):
        self.data, self.executed = data, []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, sql, params):
        assert sql in a.SQL.values() and sql.count('%s') == len(params)
        self.name = next(k for k, v in a.SQL.items() if v == sql)
        self.executed.append(self.name)
    def fetchmany(self, limit):
        assert limit <= a.SAMPLE_LIMIT + 1
        return self.data[self.name][:limit]


class Connection:
    def __init__(self, data=None):
        self.cur = Cursor(database_data() if data is None else data)
        self.closed, self.rollbacks, self.session = False, 0, None
    def cursor(self):
        return self.cur
    def set_session(self, **kwargs):
        self.session = kwargs
    def rollback(self):
        self.rollbacks += 1
    def close(self):
        self.closed = True


class S3AuditTests(unittest.TestCase):
    def test_pagination(self):
        client = Client({'pbp/': [page('pbp/', True, SECRET), page('pbp/')]})
        result = a.prefix_stats(a.ReadOnlyS3(client), BUCKET, 'pbp/', NOW)
        self.assertTrue(result['complete'])
        self.assertEqual((result['pages'], result['objects_observed'], result['bytes_observed']), (2, 2, 14))
        self.assertEqual(result['objects_24h_observed'], 2)
        self.assertEqual(client.calls[1][1]['ContinuationToken'], SECRET)
        self.assertNotIn(SECRET, a.serialize(result))

    def test_modification_window_and_partial_report(self):
        response = page('pbp/')
        response['Contents'].append(dict(Key='pbp/older.parquet', Size=3,
                                        LastModified=NOW - dt.timedelta(days=2)))
        response['KeyCount'] = 2
        result = a.prefix_stats(a.ReadOnlyS3(Client({'pbp/': [response]})), BUCKET, 'pbp/', NOW)
        self.assertEqual((result['objects_observed'], result['bytes_observed'],
                          result['objects_24h_observed']), (2, 10, 1))
        self.assertEqual(result['latest_modified_utc'], NOW.isoformat())
        client = Client({'pbp/': [page('pbp/', True, 'continue')]})
        with patch.object(a, 'MAX_PAGES', 1):
            result = a.report(a.ReadOnlyS3(client), BUCKET, NOW, {'unique_pbp_paths': 10}, [])
        self.assertFalse(result['complete'])
        self.assertFalse(result['all_unique_paths_checked'])
        self.assertEqual(result['prefixes'][-1]['objects_observed'], 1)
        self.assertFalse(result['prefixes'][-1]['complete'])
        result = a.prefix_stats(a.ReadOnlyS3(Client({'pbp/': [TimeoutError(SECRET)]})), BUCKET, 'pbp/', NOW)
        self.assertEqual(result['reason'], 'transport_error')
        self.assertNotIn(SECRET, a.serialize(result))

    def test_freshness_empty_past_and_future(self):
        empty_result = a.prefix_stats(a.ReadOnlyS3(Client()), BUCKET, 'pbp/', NOW)
        self.assertIsNone(empty_result['latest_age_hours'])
        self.assertIsNone(empty_result['latest_modified_utc'])
        self.assertEqual((empty_result['complete'], empty_result['reason'], empty_result['count_kind']),
                         (True, 'ok', 'total'))
        for offset, expected_age, recent in [(dt.timedelta(days=-2), 48, 0),
                                             (dt.timedelta(hours=2), -2, 1)]:
            response = page('pbp/')
            response['Contents'][0]['LastModified'] = NOW + offset
            result = a.prefix_stats(a.ReadOnlyS3(Client({'pbp/': [response]})), BUCKET, 'pbp/', NOW)
            self.assertEqual(result['latest_age_hours'], expected_age)
            self.assertEqual(result['objects_24h_observed'], recent)
            self.assertTrue(result['complete'])  # No cadence-based error inferred.
            self.assertEqual(result['reason'], 'ok')
        self.assertLess(a.latest_age_hours(NOW, NOW + dt.timedelta(microseconds=1)), 0)
        self.assertEqual(a.latest_age_hours(NOW, NOW.astimezone(dt.timezone(dt.timedelta(hours=3)))), 0)
        for invalid in (SECRET, NOW.date(), NOW.replace(tzinfo=None),
                        dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                        dt.datetime.max.replace(tzinfo=dt.timezone.utc)):
            with self.assertRaises(a.AuditFailure):
                a.latest_age_hours(NOW, invalid)
            with self.assertRaises(a.AuditFailure):
                a.latest_age_hours(invalid, None)
        self.assertGreater(a.latest_age_hours(NOW, dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)), 0)

    def test_missing_last_modified_keeps_only_validated_pages(self):
        first, second = page('pbp/', True, 'continue'), page('pbp/')
        first['Contents'][0]['LastModified'] = NOW - dt.timedelta(hours=2)
        del second['Contents'][0]['LastModified']
        result = a.prefix_stats(a.ReadOnlyS3(Client({'pbp/': [first, second]})), BUCKET, 'pbp/', NOW)
        self.assertEqual((result['complete'], result['reason'], result['count_kind']),
                         (False, 'response_invalid', 'lower_bound'))
        self.assertEqual((result['objects_observed'], result['bytes_observed'], result['latest_age_hours']), (1, 7, 2))
        result = a.prefix_stats(a.ReadOnlyS3(Client({'pbp/': [second]})), BUCKET, 'pbp/', NOW)
        self.assertIsNone(result['latest_age_hours'])
        self.assertIsNone(result['latest_modified_utc'])
        self.assertEqual(result['objects_observed'], 0)
        self.assertNotIn(SECRET, a.serialize(result))

    def test_partial_budget_counters_are_lower_bounds(self):
        for setting, reason in [('MAX_PAGES', 'page_limit'), ('MAX_CALLS', 'request_limit'),
                                ('MAX_HTTP_ATTEMPTS', 'http_attempt_limit')]:
            client = Client({'pbp/': [page('pbp/', True, 'continue'), page('pbp/')]})
            with patch.object(a, setting, 1):
                result = a.prefix_stats(a.ReadOnlyS3(client), BUCKET, 'pbp/', NOW)
            self.assertEqual((result['complete'], result['reason'], result['count_kind']),
                             (False, reason, 'lower_bound'))
            self.assertEqual((result['objects_observed'], result['bytes_observed']), (1, 7))
            self.assertEqual(result['latest_age_hours'], 0)
        for status, reason in [(403, 'access_denied'), (404, 'not_found')]:
            result = a.prefix_stats(a.ReadOnlyS3(Client({'pbp/': [S3Error(status)]})), BUCKET, 'pbp/', NOW)
            self.assertFalse(result['complete'])
            self.assertEqual(result['reason'], reason)
            self.assertEqual(result['count_kind'], 'lower_bound')

    def test_unavailable_reason_accounting(self):
        keys = ['pbp/' + str(i) + '.parquet' for i in range(7)]
        client = Client(heads={keys[1]: S3Error(404), keys[2]: S3Error(403),
                              keys[3]: TimeoutError(SECRET), keys[4]: {},
                              keys[5]: S3Error(500), keys[6]: a.AuditFailure('operation_forbidden')})
        result = a.head_sample(a.ReadOnlyS3(client), BUCKET, keys)
        self.assertEqual(result['unavailable_by_reason'],
                         dict(access_denied=1, transport_error=1, response_invalid=1, service_error=2))
        self.assertEqual(result['unavailable'], sum(result['unavailable_by_reason'].values()))
        self.assertEqual(result['checked'], result['found'] + result['missing'] + result['unavailable'])
        self.assertEqual((result['found'], result['missing'], result['unavailable']), (1, 1, 5))
        text = a.serialize(result)
        for forbidden in [SECRET, BUCKET, 'https://', 's3://', 'pbp/0.parquet', 'Traceback']:
            self.assertNotIn(forbidden, text)
        with patch.object(a, 'MAX_CALLS', 1):
            result = a.head_sample(a.ReadOnlyS3(Client(heads={keys[0]: S3Error(403)})), BUCKET, keys[:2])
        self.assertEqual((result['checked'], result['unavailable'], result['reason']), (1, 1, 'request_limit'))
        self.assertEqual(sum(result['unavailable_by_reason'].values()), 1)
        result = a.head_sample(a.ReadOnlyS3(Client(heads={keys[0]: S3Error(404)})), BUCKET, keys[:1])
        self.assertEqual((result['missing'], result['unavailable'], sum(result['unavailable_by_reason'].values())), (1, 0, 0))

    def test_empty_bucket(self):
        result = a.report(a.ReadOnlyS3(Client()), BUCKET, NOW, {'unique_pbp_paths': 0}, [])
        self.assertTrue(result['complete'])
        self.assertTrue(result['all_unique_paths_checked'])
        self.assertTrue(all(p['objects_observed'] == 0 and p['latest_modified_utc'] is None for p in result['prefixes']))

    def test_incomplete_and_token_loop(self):
        for pages, reason in [([page('pbp/', True, 'a')], 'page_limit'),
                              ([page('pbp/', True, 'a'), page('pbp/', True, 'a')], 'response_invalid'),
                              ([page('pbp/', True, 'a'), S3Error(403)], 'access_denied')]:
            client = Client({'pbp/': pages})
            with patch.object(a, 'MAX_PAGES', 1 if reason == 'page_limit' else 20):
                result = a.prefix_stats(a.ReadOnlyS3(client), BUCKET, 'pbp/', NOW)
            self.assertFalse(result['complete'])
            self.assertEqual(result['reason'], reason)
            self.assertEqual(result['objects_observed'], 1)
            self.assertNotIn(SECRET, a.serialize(result))

    def test_request_time_and_wire_limits(self):
        for attribute, value, reason in [('MAX_CALLS', 0, 'request_limit'),
                                         ('MAX_HTTP_ATTEMPTS', 0, 'http_attempt_limit')]:
            with patch.object(a, attribute, value):
                result = a.prefix_stats(a.ReadOnlyS3(Client()), BUCKET, 'pbp/', NOW)
            self.assertEqual(result['reason'], reason)
            self.assertFalse(result['complete'])
        clock = [0]
        s3 = a.ReadOnlyS3(Client(), clock=lambda: clock[0])
        clock[0] = a.MAX_SECONDS
        self.assertEqual(a.prefix_stats(s3, BUCKET, 'pbp/', NOW)['reason'], 'time_limit')
        with self.assertRaises(a.AuditFailure):
            a.alarm_handler(None, None)

    def test_head_outcomes_and_deduplication(self):
        keys = ['pbp/found.parquet', 'pbp/missing.parquet', 'pbp/denied.parquet', 'pbp/timeout.parquet']
        client = Client(heads={keys[1]: S3Error(404), keys[2]: S3Error(403), keys[3]: TimeoutError(SECRET)})
        result = a.head_sample(a.ReadOnlyS3(client), BUCKET, keys + keys[:1])
        self.assertEqual([result[k] for k in ('checked', 'found', 'missing', 'unavailable')], [4, 1, 1, 2])
        self.assertFalse(result['complete'])
        self.assertEqual(len(client.calls), 4)
        self.assertNotIn('pbp/', a.serialize(result))
        self.assertNotIn(SECRET, a.serialize(result))

    def test_no_following_urls_or_outside_prefix_paths(self):
        client = Client()
        result = a.head_sample(a.ReadOnlyS3(client), BUCKET, ['https://' + SECRET, 's3://elsewhere/pbp/x.parquet', 'other/x.parquet'])
        self.assertEqual(result['invalid_paths'], 3)
        self.assertFalse(result['complete'])
        self.assertEqual(client.calls, [])
        with self.assertRaises(a.AuditFailure):
            a.head_sample(a.ReadOnlyS3(client), BUCKET, ['pbp/x.parquet'] * 25)

    def test_write_operations_blocked(self):
        s3 = a.ReadOnlyS3(Client())
        for operation in ('PutObject', 'DeleteObject', 'PutObjectAcl', 'GetObject'):
            with self.assertRaises(a.AuditFailure):
                s3.invoke(operation, Bucket=BUCKET)
            with self.assertRaises(a.AuditFailure):
                s3.before_call(types.SimpleNamespace(name=operation))
        self.assertEqual(s3.calls, 0)

    def test_response_validation_and_serialization_limit(self):
        for field, value in [('Size', True), ('Size', -1), ('LastModified', SECRET),
                             ('LastModified', NOW.replace(tzinfo=None)), ('Key', 'outside-prefix')]:
            response = page('pbp/')
            response['Contents'][0][field] = value
            result = a.prefix_stats(a.ReadOnlyS3(Client({'pbp/': [response]})), BUCKET, 'pbp/', NOW)
            self.assertEqual(result['reason'], 'response_invalid')
            self.assertEqual(result['objects_observed'], 0)
        first, second = page('pbp/', True, 'continue'), page('pbp/')
        first['Contents'][0]['Size'] = 2**63 - 1
        result = a.prefix_stats(a.ReadOnlyS3(Client({'pbp/': [first, second]})), BUCKET, 'pbp/', NOW)
        self.assertEqual(result['reason'], 'response_invalid')
        self.assertEqual((result['objects_observed'], result['bytes_observed']), (1, 2**63 - 1))
        with self.assertRaises(a.AuditFailure):
            a.serialize({'test': 'x' * a.MAX_OUTPUT})
        self.assertEqual(a.code(a.AuditFailure([SECRET])), 'audit_failed')
        self.assertEqual(a.code(RuntimeError(SECRET)), 'service_error')
        client = Client(heads={'pbp/bad.parquet': {'ContentLength': SECRET}})
        result = a.head_sample(a.ReadOnlyS3(client), BUCKET, ['pbp/bad.parquet'])
        self.assertEqual(result['unavailable'], 1)
        self.assertFalse(result['complete'])

    def test_db_allowlist_duplicates_and_missing_schema(self):
        data = database_data()
        data['sample'].append(data['sample'][0])
        connection = Connection(data)
        now, counts, paths = a.database_snapshot(connection)
        self.assertEqual(now, NOW)
        self.assertEqual((counts['with_pbp_path'], counts['unique_pbp_paths'], len(paths)), (3, 2, 2))
        self.assertEqual(connection.cur.executed, list(a.SQL))
        for sql in a.SQL.values():
            self.assertTrue(sql.lstrip().startswith('SELECT '))
            self.assertNotIn(';', sql)
            self.assertNotRegex(sql.upper(), r'\b(INSERT|UPDATE|DELETE|COPY|CALL|ALTER|CREATE|DROP)\b')
        data['schema'] = []
        with self.assertRaises(a.AuditFailure):
            a.database_snapshot(Connection(data))
        data = database_data()
        data['session'] = [('off', 'repeatable read', NOW.replace(tzinfo=None))]
        with self.assertRaises(a.AuditFailure):
            a.database_snapshot(Connection(data))

    def run_main(self, dependencies=True, fail_db=False):
        values = dict(POSTGRES_HOST='localhost', POSTGRES_PORT='5432', POSTGRES_USER='test',
                      POSTGRES_PASSWORD=SECRET, POSTGRES_DB='test',
                      S3_ENDPOINT_URL='https://storage.yandexcloud.net', S3_REGION='ru-central1',
                      S3_BUCKET=BUCKET, S3_ACCESS_KEY_ID=SECRET, S3_SECRET_ACCESS_KEY=SECRET)
        connection, client = Connection(), Client()
        driver = types.ModuleType('psycopg2')
        def connect(**kwargs):
            self.assertIn('default_transaction_read_only=on', kwargs['options'])
            self.assertIn('statement_timeout=5000', kwargs['options'])
            self.assertIn('lock_timeout=1000', kwargs['options'])
            if fail_db:
                raise RuntimeError(SECRET)
            return connection
        driver.connect = connect
        sdk = types.ModuleType('boto3')
        def make_client(*args, **kwargs):
            self.assertTrue(connection.closed)  # No DB locks during S3 audit.
            self.assertTrue(kwargs['verify'])
            self.assertEqual(kwargs['config']['retries']['total_max_attempts'], 2)
            self.assertEqual(kwargs['config']['connect_timeout'], 2)
            self.assertEqual(kwargs['config']['read_timeout'], 3)
            return client
        sdk.client = make_client
        config = types.ModuleType('botocore.config')
        config.Config = lambda **kwargs: kwargs
        out = io.StringIO()
        with patch.dict(sys.modules, {'psycopg2': driver, 'boto3': sdk,
                                     'botocore': types.ModuleType('botocore'), 'botocore.config': config}), \
             patch.object(a.importlib.util, 'find_spec', return_value=object() if dependencies else None), \
             patch.object(a, 'configuration', return_value=(values, 5432)), \
             patch.object(a.signal, 'signal'), patch.object(a.signal, 'alarm'), \
             patch.object(a.logging, 'disable'), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = a.main()
        self.assertNotIn(SECRET, out.getvalue())
        return status, json.loads(out.getvalue()), connection, client

    def test_main_safe_output_and_cleanup(self):
        status, report, connection, client = self.run_main()
        self.assertEqual(status, 0)
        self.assertTrue(report['complete'])
        self.assertTrue(connection.closed and client.closed)
        self.assertGreater(connection.rollbacks, 0)
        self.assertEqual(connection.session, dict(readonly=True, isolation_level='REPEATABLE READ', autocommit=False))
        self.assertEqual(self.run_main(dependencies=False)[:2],
                         (1, {'complete': False, 'audit_status': 'dependency_unavailable'}))
        self.assertEqual(self.run_main(fail_db=True)[:2],
                         (1, {'complete': False, 'audit_status': 'db_failed'}))

    def test_strict_config(self):
        values = {key: 'literal' for key in a.CONNECTION_FIELDS}
        values.update(POSTGRES_PORT='5432', S3_ENDPOINT_URL='https://storage.yandexcloud.net',
                      S3_REGION='ru-central1', S3_BUCKET=BUCKET)
        self.assertEqual(a.parse_env('S3_SECRET_ACCESS_KEY="literal # value"')['S3_SECRET_ACCESS_KEY'], 'literal # value')
        text = '\n'.join(k + '=' + v for k, v in values.items() if k != 'S3_SECRET_ACCESS_KEY')
        with patch.object(Path, 'open', return_value=io.BytesIO(text.encode())), \
             patch.dict(a.os.environ, {}, clear=True), self.assertRaises(a.AuditFailure):
            a.configuration()
        with patch.object(a, 'connection_values', return_value=(values, 5432)):
            self.assertEqual(a.configuration()[1], 5432)
            for key, bad in [('S3_ENDPOINT_URL', 'https://' + SECRET), ('S3_BUCKET', '../invalid'),
                             ('S3_BUCKET', '127.0.0.1'), ('S3_REGION', 'unexpected')]:
                with patch.dict(values, {key: bad}), self.assertRaises(a.AuditFailure):
                    a.configuration()


if __name__ == '__main__':
    unittest.main()
