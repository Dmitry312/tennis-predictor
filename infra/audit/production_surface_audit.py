"""Full, bounded read-only surface-marker coverage audit; no app imports."""
import csv
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import unicodedata

CONNECTION_FIELDS = ('POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB')
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_NAME_BYTES = 65536
HEADER = ('tournament_name', 'matches_count', 'surface_detected', 'matched_markers',
          'first_match_date', 'last_match_date')
CATEGORIES = ('hard', 'clay', 'grass', 'unknown', 'conflict')
MARKERS = {'хард': 'hard', 'hard': 'hard', 'грунт': 'clay', 'clay': 'clay',
           'трава': 'grass', 'grass': 'grass'}
# Python's default Unicode-aware word characters; no ASCII/JavaScript boundaries.
PATTERN = re.compile(r'(?<!\w)(хард|hard|грунт|clay|трава|grass)(?!\w)')
EXPECTED = {'tournament_name': 'varchar', 'match_date': 'date',
            'surface': 'varchar', 'duplicate_of': 'uuid'}
SQL = {
    'session': "SELECT current_setting(%s), current_setting(%s), CURRENT_TIMESTAMP AT TIME ZONE %s",
    'schema': """SELECT a.attname, t.typname FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
        WHERE n.nspname=%s AND c.relname=%s AND a.attname=ANY(%s)
          AND a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p')
          AND t.typnamespace='pg_catalog'::pg_catalog.regnamespace""",
    'totals': """SELECT COUNT(*), COUNT(*) FILTER (WHERE duplicate_of IS NULL),
        COUNT(DISTINCT tournament_name COLLATE "C"),
        COUNT(DISTINCT tournament_name COLLATE "C") FILTER (WHERE duplicate_of IS NULL)
        FROM public.matches""",
    'names': """SELECT tournament_name COLLATE "C", COUNT(*),
        COUNT(*) FILTER (WHERE duplicate_of IS NULL), MIN(match_date), MAX(match_date),
        MIN(match_date) FILTER (WHERE duplicate_of IS NULL),
        MAX(match_date) FILTER (WHERE duplicate_of IS NULL),
        COUNT(*) FILTER (WHERE surface IS NOT NULL AND btrim(surface)<>%s),
        COUNT(*) FILTER (WHERE duplicate_of IS NULL AND surface IS NOT NULL AND btrim(surface)<>%s)
        FROM public.matches GROUP BY 1 ORDER BY 1 NULLS FIRST""",
}
FILES = tuple(scope + suffix + '.csv' for scope in ('all', 'canonical')
              for suffix in ('_tournaments', '_unknown', '_conflict'))
CODES = frozenset(('connection_config_unavailable', 'dependency_unavailable',
                  'readonly_not_confirmed', 'schema_mismatch', 'unexpected_result',
                  'export_limit', 'audit_timeout', 'query_timeout', 'lock_timeout',
                  'table_unavailable', 'connection_or_query_failed', 'artifact_invalid'))


class AuditFailure(Exception):
    pass


def parse_env(text):
    """Small literal .env subset, not a shell/dotenv interpreter.

    Only the five connection keys are parsed. Single/double quoted literal values
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


def classify(name):
    if name is None:
        return 'unknown', ()
    if type(name) is not str or len(name.encode('utf-8')) > MAX_NAME_BYTES:
        raise AuditFailure('unexpected_result')
    normalized = unicodedata.normalize('NFKC', name).casefold()
    markers = tuple(sorted({match[1] for match in PATTERN.finditer(normalized)
                            if not (match.start() and unicodedata.category(normalized[match.start()-1]).startswith('M'))
                            and not (match.end() < len(normalized) and unicodedata.category(normalized[match.end()]).startswith('M'))}))
    surfaces = {MARKERS[marker] for marker in markers}
    return ('unknown' if not surfaces else next(iter(surfaces)) if len(surfaces) == 1 else 'conflict'), markers


def formula_risk(value):
    probe = unicodedata.normalize('NFKC', value)
    while probe and (probe[0].isspace() or unicodedata.category(probe[0]) in ('Cc', 'Cf')):
        probe = probe[1:]
    return bool(probe and probe[0] in '=+-@') or bool(value and unicodedata.category(value[0]) in ('Cc', 'Cf'))


def csv_name(name):
    # Reversible NULL/backslash/apostrophe encoding plus formula-injection guard.
    if name is None:
        return r'\N'
    if name.startswith('\\') or name.startswith("'"):
        return name[0] + name
    return "'" + name if formula_risk(name) else name


def original_name(cell):
    if cell == r'\N':
        return None
    if cell.startswith('\\\\') or cell.startswith("'"):
        return cell[1:]
    return cell


def count(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise AuditFailure('unexpected_result')
    return value


def date_text(value):
    if value is None:
        return ''
    if type(value) is not dt.date or value in (dt.date.min, dt.date.max):
        raise AuditFailure('unexpected_result')
    return value.isoformat()


def percentage(part, total):
    if not total:
        return 0.0
    return float((Decimal(part) * 100 / Decimal(total)).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP))


def scope_summary():
    return dict(matches_total=0, unique_names=0, name_groups_including_null=0,
                null_name_matches=0, blank_name_matches=0, existing_surface_nonempty=0,
                categories={category: 0 for category in CATEGORIES})


class Export:
    def __init__(self):
        self.summary = {scope: scope_summary() for scope in ('all', 'canonical')}
        self.streams = {name: io.StringIO(newline='') for name in FILES}
        self.csv_bytes = 0
        for name in FILES:
            self.write(name, HEADER)

    def write(self, name, row):
        # Serialize one bounded row before adding it to the complete export.
        stream = io.StringIO(newline='')
        csv.writer(stream, lineterminator='\n').writerow(row)
        text = stream.getvalue()
        self.csv_bytes += len(text.encode('utf-8'))
        if self.csv_bytes > MAX_CSV_BYTES:
            raise AuditFailure('export_limit')
        self.streams[name].write(text)

    def add(self, scope, name, matches, first, last, surface_count):
        detected, markers = classify(name)
        matches, surface_count = count(matches), count(surface_count)
        if not matches or surface_count > matches:
            raise AuditFailure('unexpected_result')
        first_text, last_text = date_text(first), date_text(last)
        if bool(first_text) != bool(last_text) or (first is not None and first > last):
            raise AuditFailure('unexpected_result')
        info = self.summary[scope]
        info['matches_total'] += matches
        info['name_groups_including_null'] += 1
        info['unique_names'] += int(name is not None)
        info['null_name_matches'] += matches if name is None else 0
        info['blank_name_matches'] += matches if name is not None and not name.strip() else 0
        info['existing_surface_nonempty'] += surface_count
        info['categories'][detected] += matches
        row = (csv_name(name), matches, detected, ';'.join(markers), first_text, last_text)
        self.write(scope + '_tournaments.csv', row)
        if detected in ('unknown', 'conflict'):
            self.write(scope + '_' + detected + '.csv', row)

    def finish(self, now, expected):
        for scope, total, unique in (('all', expected[0], expected[2]), ('canonical', expected[1], expected[3])):
            info = self.summary[scope]
            if info['matches_total'] != total or info['unique_names'] != unique:
                raise AuditFailure('unexpected_result')
            info['categories'] = {label: {'matches': n, 'percent': percentage(n, total)}
                                  for label, n in info['categories'].items()}
            if sum(item['matches'] for item in info['categories'].values()) != total:
                raise AuditFailure('unexpected_result')
            classified = sum(info['categories'][label]['matches'] for label in ('hard', 'clay', 'grass'))
            info['automatically_classified_matches'] = classified
            info['automatically_classified_percent'] = percentage(classified, total)
        return {'complete': True, 'summary': {'audit_utc': now.isoformat() + 'Z',
                'scopes': self.summary, 'classifier': 'unicode_nfkc_casefold_word_markers_v1'},
                'csv': {name: stream.getvalue() for name, stream in self.streams.items()}}


def small_query(cursor, name, params, limit):
    cursor.execute(SQL[name], params)
    result = cursor.fetchmany(limit + 1)
    if len(result) > limit:
        raise AuditFailure('unexpected_result')
    return result


def audit(connection):
    with connection.cursor() as cursor:
        session = small_query(cursor, 'session', ('transaction_read_only', 'transaction_isolation', 'UTC'), 1)
        if len(session) != 1 or session[0][:2] != ('on', 'repeatable read'):
            raise AuditFailure('readonly_not_confirmed')
        now = session[0][2]
        if type(now) is not dt.datetime or now.tzinfo is not None:
            raise AuditFailure('unexpected_result')
        schema = small_query(cursor, 'schema', ('public', 'matches', list(EXPECTED)), len(EXPECTED))
        if dict(schema) != EXPECTED:
            raise AuditFailure('schema_mismatch')
        totals = small_query(cursor, 'totals', (), 1)
        if len(totals) != 1 or len(totals[0]) != 4:
            raise AuditFailure('unexpected_result')
        expected = [count(value) for value in totals[0]]
        if expected[1] > expected[0] or expected[2] > expected[0] or expected[3] > expected[1]:
            raise AuditFailure('unexpected_result')
    export = Export()
    # Server-side cursor limits client memory, not coverage: SQL has no LIMIT.
    with connection.cursor(name='surface_audit_names', scrollable=False, withhold=False) as cursor:
        cursor.itersize = 256
        cursor.execute(SQL['names'], ('', ''))
        while True:
            batch = cursor.fetchmany(256)
            if not batch:
                break
            for row in batch:
                if len(row) != 9:
                    raise AuditFailure('unexpected_result')
                name, total, canonical, first, last, cfirst, clast, filled, cfilled = row
                total, canonical = count(total), count(canonical)
                filled, cfilled = count(filled), count(cfilled)
                if not total or canonical > total or (not canonical and (cfirst is not None or clast is not None or cfilled != 0)):
                    raise AuditFailure('unexpected_result')
                export.add('all', name, total, first, last, filled)
                if canonical:
                    export.add('canonical', name, canonical, cfirst, clast, cfilled)
    return export.finish(now, expected)


def serialize(value):
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(text.encode('utf-8')) > MAX_EXPORT_BYTES:
        raise AuditFailure('export_limit')
    return text


def expired(signum, frame):
    raise AuditFailure('audit_timeout')


def remote():
    connection = None
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(50)
    try:
        if importlib.util.find_spec('psycopg2') is None:
            raise AuditFailure('dependency_unavailable')
        try:
            import psycopg2
        except ImportError:
            raise AuditFailure('dependency_unavailable') from None
        values, port = connection_values()
        connection = psycopg2.connect(
            host=values['POSTGRES_HOST'], port=port, user=values['POSTGRES_USER'],
            password=values['POSTGRES_PASSWORD'], dbname=values['POSTGRES_DB'],
            connect_timeout=5, application_name='production-surface-audit',
            options='-c default_transaction_read_only=on -c statement_timeout=5000 '
                    '-c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000 '
                    '-c search_path=pg_catalog -c timezone=UTC')
        connection.set_session(readonly=True, isolation_level='REPEATABLE READ', autocommit=False)
        payload = serialize(audit(connection))
        connection.rollback()
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdout.write(payload)  # Redirected away from logs; no extra byte past cap.
        return 0
    except AuditFailure as error:
        candidate = error.args[0] if error.args else None
        status = candidate if type(candidate) is str and candidate in CODES else 'connection_or_query_failed'
    except Exception as error:
        status = {'57014': 'query_timeout', '55P03': 'lock_timeout', '42P01': 'table_unavailable',
                  '42703': 'schema_mismatch'}.get(getattr(error, 'pgcode', None), 'connection_or_query_failed')
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
    print(json.dumps({'complete': False, 'audit_status': status}))
    return 1


def unpack(source, destination):
    # Runner-only. No remote files or DB access; accept fixed filenames only.
    try:
        with Path(source).open('rb') as stream:
            raw = stream.read(MAX_EXPORT_BYTES + 1)
        if len(raw) > MAX_EXPORT_BYTES:
            raise AuditFailure('export_limit')
        def reject_constant(value):
            raise AuditFailure('artifact_invalid')
        envelope = json.loads(raw, parse_constant=reject_constant)
        if type(envelope) is not dict or set(envelope) != {'complete', 'summary', 'csv'} or envelope['complete'] is not True:
            raise AuditFailure('artifact_invalid')
        documents = envelope['csv']
        if type(documents) is not dict or set(documents) != set(FILES):
            raise AuditFailure('artifact_invalid')
        if any(type(text) is not str for text in documents.values()) or sum(len(t.encode('utf-8')) for t in documents.values()) > MAX_CSV_BYTES:
            raise AuditFailure('export_limit')
        csv.field_size_limit(MAX_NAME_BYTES + 2)
        parsed = {}
        for name, text in documents.items():
            parsed[name] = []
            reader = csv.reader(io.StringIO(text, newline=''), strict=True)
            if next(reader, None) != list(HEADER):
                raise AuditFailure('artifact_invalid')
            for row in reader:
                if len(row) != len(HEADER) or not re.fullmatch(r'[1-9][0-9]{0,12}', row[1]):
                    raise AuditFailure('artifact_invalid')
                original = original_name(row[0])
                detected, markers = classify(original)
                if csv_name(original) != row[0] or (row[2], row[3]) != (detected, ';'.join(markers)):
                    raise AuditFailure('artifact_invalid')
                if name.endswith('_unknown.csv') and detected != 'unknown' or name.endswith('_conflict.csv') and detected != 'conflict':
                    raise AuditFailure('artifact_invalid')
                dates = [dt.date.fromisoformat(value) if value else None for value in row[4:]]
                if bool(dates[0]) != bool(dates[1]) or (dates[0] is not None and dates[0] > dates[1]):
                    raise AuditFailure('artifact_invalid')
                parsed[name].append((original, count(int(row[1])), *dates))
        summary = envelope['summary']
        # Do not echo names or arbitrary remotely supplied summary text to logs.
        if type(summary) is not dict or set(summary) != {'audit_utc', 'scopes', 'classifier'} or summary['classifier'] != 'unicode_nfkc_casefold_word_markers_v1':
            raise AuditFailure('artifact_invalid')
        if type(summary['scopes']) is not dict or set(summary['scopes']) != {'all', 'canonical'}:
            raise AuditFailure('artifact_invalid')
        if type(summary['audit_utc']) is not str or not summary['audit_utc'].endswith('Z'):
            raise AuditFailure('artifact_invalid')
        now = dt.datetime.fromisoformat(summary['audit_utc'][:-1])
        if now.tzinfo is not None:
            raise AuditFailure('artifact_invalid')
        rebuilt = Export()
        seen = {}
        for scope in ('all', 'canonical'):
            seen[scope] = {}
            for original, matches, first, last in parsed[scope + '_tournaments.csv']:
                if original in seen[scope]:
                    raise AuditFailure('artifact_invalid')
                seen[scope][original] = matches
                rebuilt.add(scope, original, matches, first, last, 0)
        for original, matches in seen['canonical'].items():
            if original not in seen['all'] or matches > seen['all'][original]:
                raise AuditFailure('artifact_invalid')
        scopes = summary['scopes']
        expected = (count(scopes['all']['matches_total']), count(scopes['canonical']['matches_total']),
                    count(scopes['all']['unique_names']), count(scopes['canonical']['unique_names']))
        verified = rebuilt.finish(now, expected)
        for scope in ('all', 'canonical'):
            existing = count(scopes[scope]['existing_surface_nonempty'])
            if existing > expected[0 if scope == 'all' else 1]:
                raise AuditFailure('artifact_invalid')
            verified['summary']['scopes'][scope]['existing_surface_nonempty'] = existing
        if verified['summary'] != summary or verified['csv'] != documents:
            raise AuditFailure('artifact_invalid')
        target = Path(destination)
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, text in documents.items():
            (target / name).write_text(text, encoding='utf-8', newline='')
        (target / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        print('SURFACE_AUDIT_EXPORT_COMPLETE')
        return 0
    except Exception:
        print('SURFACE_AUDIT_ARTIFACT_FAILED')
        return 1


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--unpack':
        sys.exit(unpack(sys.argv[2], sys.argv[3]))
    if len(sys.argv) != 1:
        sys.exit(1)
    sys.exit(remote())
