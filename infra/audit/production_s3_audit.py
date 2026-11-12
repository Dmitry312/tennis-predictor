"""Bounded S3 metadata / PostgreSQL reference audit; no application imports."""
import datetime as dt
import importlib.util
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sys
import time

PREFIXES = ('nbbet_details/', 'nbbet_stats/', 'nbbet_raw/', 'pbp/')
UNAVAILABLE_REASONS = ('access_denied', 'transport_error', 'response_invalid', 'service_error')
SAMPLE_LIMIT = 24
PAGE_SIZE = 500
MAX_PAGES = 20
MAX_CALLS = 120
MAX_HTTP_ATTEMPTS = 160
MAX_SECONDS = 90
MAX_OUTPUT = 16384
CONNECTION_FIELDS = ('POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_USER',
                     'POSTGRES_PASSWORD', 'POSTGRES_DB', 'S3_ENDPOINT_URL',
                     'S3_BUCKET', 'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY', 'S3_REGION')
CODES = frozenset(('config_invalid', 'connection_config_unavailable', 'dependency_unavailable',
                  'db_failed', 'schema_mismatch', 'db_timeout', 'readonly_not_confirmed',
                  'response_invalid', 'page_limit', 'request_limit', 'http_attempt_limit',
                  'time_limit', 'access_denied', 'not_found', 'service_error', 'transport_error',
                  'operation_forbidden', 'output_limit', 'audit_failed'))
SQL = {
    'session': "SELECT current_setting(%s), current_setting(%s), CURRENT_TIMESTAMP AT TIME ZONE %s LIMIT %s",
    'schema': """SELECT t.typname, c.relkind FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
        JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
        WHERE n.nspname=%s AND c.relname=%s AND a.attname=%s
          AND a.attnum>0 AND NOT a.attisdropped
          AND t.typnamespace='pg_catalog'::pg_catalog.regnamespace LIMIT %s""",
    'counts': """SELECT COUNT(*),
        COUNT(*) FILTER (WHERE pbp_s3_path IS NOT NULL AND pbp_s3_path<>%s),
        COUNT(DISTINCT NULLIF(pbp_s3_path,%s)),
        COUNT(*) FILTER (WHERE pbp_s3_path=%s),
        COUNT(*) FILTER (WHERE octet_length(pbp_s3_path)>%s)
        FROM public.matches LIMIT %s""",
    'sample': """SELECT DISTINCT pbp_s3_path COLLATE "C" FROM public.matches
        WHERE pbp_s3_path IS NOT NULL AND pbp_s3_path<>%s
          AND octet_length(pbp_s3_path)<=%s ORDER BY 1 LIMIT %s""",
}


class AuditFailure(Exception):
    pass


def code(error):
    if isinstance(error, AuditFailure):
        candidate = error.args[0] if error.args else None
        return candidate if type(candidate) is str and candidate in CODES else 'audit_failed'
    response = getattr(error, 'response', None)
    if type(response) is dict:
        metadata = response.get('ResponseMetadata', {})
        status = metadata.get('HTTPStatusCode') if type(metadata) is dict else None
        if type(status) is int and status in (403, 404):
            return 'access_denied' if status == 403 else 'not_found'
    if isinstance(error, TimeoutError) or type(error).__name__ in (
            'ReadTimeoutError', 'ConnectTimeoutError', 'EndpointConnectionError'):
        return 'transport_error'
    return 'service_error'


def count(value):
    if type(value) is not int or not 0 <= value <= 2**63-1:
        raise AuditFailure('response_invalid')
    return value


def utc(value):
    if type(value) is not dt.datetime or value.tzinfo is None:
        raise AuditFailure('response_invalid')
    try:
        value = value.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        raise AuditFailure('response_invalid') from None
    if not 1970 <= value.year <= 2100:
        raise AuditFailure('response_invalid')
    return value


def latest_age_hours(now, modified):
    now = utc(now)
    if modified is None:
        return None
    delta = now - utc(modified)
    # Exact integer microseconds before division; preserve tiny negative ages.
    microseconds = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    hours = microseconds / 3600000000
    # Both timestamps are restricted to 1970..2100 by utc().
    if not math.isfinite(hours) or abs(hours) > 131 * 366 * 24:
        raise AuditFailure('response_invalid')
    return hours


def parse_env(text):
    """Small literal .env subset, not a shell/dotenv interpreter.

    Only the explicitly listed connection keys are parsed. Single/double quoted literal values
    and unquoted values without whitespace/quotes/backslashes are accepted.
    No escaping, interpolation, multiline values or duplicate connection keys.
    """
    if len(text.encode('utf-8')) > 65536 or '\x00' in text:
        raise AuditFailure('connection_config_unavailable')
    settings = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\b(.*)$', line)
        if not match or match[1] not in CONNECTION_FIELDS:
            continue
        key, rest = match[1], match[2].lstrip()
        if key in settings or not rest.startswith('='):
            raise AuditFailure('connection_config_unavailable')
        raw = rest[1:].strip()
        if raw.startswith(('"', "'")):
            quote = raw[0]
            end = raw.find(quote, 1)
            if end < 0 or (raw[end + 1:].strip() and not raw[end + 1:].lstrip().startswith('#')):
                raise AuditFailure('connection_config_unavailable')
            value = raw[1:end]
        else:
            match = re.fullmatch(r"([^\s#\"'\\]*)(?:\s+#.*)?", raw)
            if match is None:
                raise AuditFailure('connection_config_unavailable')
            value = match[1]
        if '\\' in value or '${' in value or len(value) > 4096:
            raise AuditFailure('connection_config_unavailable')
        settings[key] = value
    return settings


def connection_values():
    try:
        with Path('/root/Tennis_pelmen_model/.env').open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise AuditFailure('connection_config_unavailable')
        settings = parse_env(raw.decode('utf-8'))
    except FileNotFoundError:
        settings = {}  # Allowed only if every required field is supplied by env.
    except (OSError, UnicodeError):
        raise AuditFailure('connection_config_unavailable') from None
    values = {key: os.environ.get(key, settings.get(key)) for key in CONNECTION_FIELDS}
    if any(type(v) is not str or not v or len(v) > 4096 or
           any(c in v for c in ('\x00', '\r', '\n')) or '${' in v for v in values.values()):
        raise AuditFailure('connection_config_unavailable')
    if not re.fullmatch(r'[0-9]{1,5}', values['POSTGRES_PORT']):
        raise AuditFailure('connection_config_unavailable')
    port = int(values['POSTGRES_PORT'])
    if not 1 <= port <= 65535:
        raise AuditFailure('connection_config_unavailable')
    return values, port


def configuration():
    values, port = connection_values()
    if values['S3_ENDPOINT_URL'] != 'https://storage.yandexcloud.net' or values['S3_REGION'] != 'ru-central1':
        raise AuditFailure('config_invalid')
    bucket = values['S3_BUCKET']
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket) or '..' in bucket:
        raise AuditFailure('config_invalid')
    try:
        ipaddress.ip_address(bucket)
    except ValueError:
        return values, port
    raise AuditFailure('config_invalid')


def query(cursor, name, params, limit):
    cursor.execute(SQL[name], params)
    result = cursor.fetchmany(limit + 1)
    if len(result) > limit:
        raise AuditFailure('response_invalid')
    return result


def database_snapshot(connection):
    with connection.cursor() as cursor:
        session = query(cursor, 'session', ('transaction_read_only', 'transaction_isolation', 'UTC', 1), 1)
        if len(session) != 1 or session[0][:2] != ('on', 'repeatable read'):
            raise AuditFailure('readonly_not_confirmed')
        now = session[0][2]
        if type(now) is not dt.datetime or now.tzinfo is not None:
            raise AuditFailure('response_invalid')
        now = utc(now.replace(tzinfo=dt.timezone.utc))
        schema = query(cursor, 'schema', ('public', 'matches', 'pbp_s3_path', 2), 2)
        if len(schema) != 1 or schema[0][0] != 'varchar' or schema[0][1] not in ('r', 'p'):
            raise AuditFailure('schema_mismatch')
        result = query(cursor, 'counts', ('', '', '', 1024, 1), 1)
        if len(result) != 1 or len(result[0]) != 5:
            raise AuditFailure('response_invalid')
        numbers = [count(x) for x in result[0]]
        total, filled, unique, empty, oversized = numbers
        if not unique <= filled <= total or empty + filled > total or oversized > filled:
            raise AuditFailure('response_invalid')
        paths = []
        for row in query(cursor, 'sample', ('', 1024, SAMPLE_LIMIT), SAMPLE_LIMIT):
            if len(row) != 1 or type(row[0]) is not str or len(row[0].encode('utf-8')) > 1024:
                raise AuditFailure('response_invalid')
            if row[0] not in paths:
                paths.append(row[0])
        if len(paths) > unique:
            raise AuditFailure('response_invalid')
        return now, dict(zip(('matches_total', 'with_pbp_path', 'unique_pbp_paths',
                              'empty_paths', 'oversized_paths'), numbers)), paths


class ReadOnlyS3:
    API = {'ListObjectsV2': 'list_objects_v2', 'HeadObject': 'head_object', 'HeadBucket': 'head_bucket'}

    def __init__(self, client, clock=time.monotonic, deadline=None):
        self.client, self.clock = client, clock
        self.deadline = clock() + MAX_SECONDS if deadline is None else deadline
        self.calls = self.http_attempts = 0
        client.meta.events.register('before-call.s3', self.before_call)
        client.meta.events.register('before-send.s3', self.before_send)

    def check_time(self):
        if self.clock() >= self.deadline:
            raise AuditFailure('time_limit')

    def before_call(self, model, **kwargs):
        if model.name not in self.API:
            raise AuditFailure('operation_forbidden')
        self.check_time()

    def before_send(self, **kwargs):
        self.check_time()
        if self.http_attempts >= MAX_HTTP_ATTEMPTS:
            raise AuditFailure('http_attempt_limit')
        self.http_attempts += 1

    def invoke(self, operation, **kwargs):
        if operation not in self.API:
            raise AuditFailure('operation_forbidden')
        self.check_time()
        if self.calls >= MAX_CALLS:
            raise AuditFailure('request_limit')
        self.calls += 1
        response = getattr(self.client, self.API[operation])(**kwargs)
        self.check_time()
        if type(response) is not dict:
            raise AuditFailure('response_invalid')
        return response


def prefix_stats(s3, bucket, prefix, now):
    report = dict(prefix=prefix, complete=False, reason='page_limit', pages=0,
                  objects_observed=0, bytes_observed=0, objects_24h_observed=0,
                  latest_modified_utc=None, latest_age_hours=None, count_kind='lower_bound')
    token, seen = None, set()
    latest = None
    try:
        now = utc(now)
        for _ in range(MAX_PAGES):
            args = dict(Bucket=bucket, Prefix=prefix, MaxKeys=PAGE_SIZE)
            if token is not None:
                args['ContinuationToken'] = token
            response = s3.invoke('ListObjectsV2', **args)
            content = response.get('Contents', [])
            truncated = response.get('IsTruncated')
            if type(content) is not list or len(content) > PAGE_SIZE or type(truncated) is not bool:
                raise AuditFailure('response_invalid')
            if count(response.get('KeyCount')) != len(content):
                raise AuditFailure('response_invalid')
            size, recent, page_latest = 0, 0, None
            for item in content:
                if type(item) is not dict:
                    raise AuditFailure('response_invalid')
                key = item.get('Key')
                if type(key) is not str or not key.startswith(prefix) or len(key.encode('utf-8')) > 1024:
                    raise AuditFailure('response_invalid')
                size += count(item.get('Size'))
                modified = utc(item.get('LastModified'))
                recent += int(modified >= now - dt.timedelta(days=1))
                page_latest = modified if page_latest is None else max(page_latest, modified)
            next_token = response.get('NextContinuationToken') if truncated else None
            if truncated and (type(next_token) is not str or not 1 <= len(next_token) <= 4096 or next_token in seen):
                raise AuditFailure('response_invalid')
            # Validate cumulative counters before committing any part of a page.
            objects = count(report['objects_observed'] + len(content))
            total_bytes = count(report['bytes_observed'] + size)
            total_recent = count(report['objects_24h_observed'] + recent)
            candidate_latest = latest
            if page_latest is not None:
                candidate_latest = page_latest if latest is None else max(latest, page_latest)
            candidate_age = latest_age_hours(now, candidate_latest)
            report.update(pages=report['pages'] + 1, objects_observed=objects,
                          bytes_observed=total_bytes, objects_24h_observed=total_recent,
                          latest_modified_utc=candidate_latest.isoformat() if candidate_latest else None,
                          latest_age_hours=candidate_age)
            latest = candidate_latest
            if not truncated:
                report.update(complete=True, reason='ok', count_kind='total')
                break
            seen.add(next_token)
            token = next_token
    except Exception as error:
        report['reason'] = code(error)
    return report


def head_sample(s3, bucket, paths):
    if type(paths) is not list or len(paths) > SAMPLE_LIMIT or any(type(p) is not str for p in paths):
        raise AuditFailure('response_invalid')
    paths = list(dict.fromkeys(paths))  # Extra defence against duplicate DB rows.
    report = dict(sample_unique_paths=len(paths), checked=0, found=0, missing=0,
                  unavailable=0, invalid_paths=0, complete=False, reason='ok',
                  unavailable_by_reason={reason: 0 for reason in UNAVAILABLE_REASONS})
    for key in paths:
        if not key.startswith('pbp/') or not key.endswith('.parquet') or len(key.encode('utf-8')) > 1024 or any(ord(c) < 32 for c in key):
            report['invalid_paths'] += 1
            continue
        try:
            response = s3.invoke('HeadObject', Bucket=bucket, Key=key)
            metadata = response.get('ResponseMetadata', {})
            if type(metadata) is not dict or metadata.get('HTTPStatusCode') != 200:
                raise AuditFailure('response_invalid')
            count(response.get('ContentLength'))
            utc(response.get('LastModified'))
            report['found'] += 1
        except Exception as error:
            reason = code(error)
            if reason in ('request_limit', 'http_attempt_limit', 'time_limit'):
                report['reason'] = reason
                break
            if reason == 'not_found':
                report['missing'] += 1
            else:
                category = reason if reason in UNAVAILABLE_REASONS else 'service_error'
                report['unavailable_by_reason'][category] += 1
                report['unavailable'] += 1
        report['checked'] += 1
    if (sum(report['unavailable_by_reason'].values()) != report['unavailable'] or
            report['found'] + report['missing'] + report['unavailable'] != report['checked']):
        raise AuditFailure('response_invalid')
    report['complete'] = report['checked'] == len(paths) and not report['unavailable'] and not report['invalid_paths']
    if not report['complete'] and report['reason'] == 'ok':
        report['reason'] = 'response_invalid' if report['invalid_paths'] else 'service_error'
    return report


def report(s3, bucket, now, database, paths):
    prefixes = [prefix_stats(s3, bucket, prefix, now) for prefix in PREFIXES]
    sample = head_sample(s3, bucket, paths)
    return dict(audit_utc=now.isoformat(), complete=all(p['complete'] for p in prefixes) and sample['complete'],
                prefixes=prefixes, database=database, pbp_head_sample=sample,
                all_unique_paths_checked=sample['complete'] and sample['checked'] == database['unique_pbp_paths'],
                s3_api_calls=s3.calls, s3_http_attempts=s3.http_attempts)


def serialize(value):
    text = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    if len(text.encode('utf-8')) > MAX_OUTPUT:
        raise AuditFailure('output_limit')
    return text


def alarm_handler(signum, frame):
    raise AuditFailure('time_limit')


def main():
    connection = client = None
    deadline = time.monotonic() + MAX_SECONDS
    logging.disable(logging.CRITICAL)  # Libraries must not log credentials/errors.
    signal.signal(signal.SIGALRM, alarm_handler)
    signal.alarm(110)
    try:
        for name in ('boto3', 'botocore', 'psycopg2'):
            if importlib.util.find_spec(name) is None:
                raise AuditFailure('dependency_unavailable')
        try:
            import boto3
            from botocore.config import Config
            import psycopg2
        except ImportError:
            raise AuditFailure('dependency_unavailable') from None
        values, port = configuration()
        try:
            connection = psycopg2.connect(
                host=values['POSTGRES_HOST'], port=port, user=values['POSTGRES_USER'],
                password=values['POSTGRES_PASSWORD'], dbname=values['POSTGRES_DB'],
                connect_timeout=5, application_name='production-s3-audit',
                options='-c default_transaction_read_only=on -c statement_timeout=5000 '
                        '-c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000 '
                        '-c search_path=pg_catalog -c timezone=UTC')
            connection.set_session(readonly=True, isolation_level='REPEATABLE READ', autocommit=False)
            now, database, paths = database_snapshot(connection)
        except AuditFailure:
            raise
        except Exception as error:
            raise AuditFailure('db_timeout' if getattr(error, 'pgcode', None) in ('57014', '55P03') else 'db_failed') from None
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()
                    connection = None
        client = boto3.client('s3', endpoint_url=values['S3_ENDPOINT_URL'],
            region_name=values['S3_REGION'], aws_access_key_id=values['S3_ACCESS_KEY_ID'],
            aws_secret_access_key=values['S3_SECRET_ACCESS_KEY'], verify=True,
            config=Config(signature_version='s3v4', connect_timeout=2, read_timeout=3,
                retries={'total_max_attempts': 2, 'mode': 'standard'},
                s3={'addressing_style': 'path'}))
        result = report(ReadOnlyS3(client, deadline=deadline), values['S3_BUCKET'], now, database, paths)
        print(serialize(result))
        return 0 if result['complete'] else 1
    except Exception as error:
        print(json.dumps({'complete': False, 'audit_status': code(error)}))
        return 1
    finally:
        signal.alarm(0)
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


if __name__ == '__main__':
    sys.exit(main())
