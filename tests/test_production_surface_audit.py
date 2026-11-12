"""Offline tests only; no SSH/PostgreSQL connection or application import."""
import contextlib
import csv
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('surface_audit', ROOT / 'infra/audit/production_surface_audit.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
D = dt.date(2025, 1, 1)
E = dt.date(2025, 2, 1)
NOW = dt.datetime(2026, 11, 12, 12)
SECRET = 'PRIVATE_CONFIG_OR_ERROR_MUST_NOT_APPEAR'


def fixtures():
    names = [(None, 2, 1, D, E, D, D, 0, 0),
             ('', 1, 1, None, None, None, None, 0, 0),
             ('ATP ХАРД, final', 4, 3, D, E, E, E, 0, 0),
             ('Clay (грунт)', 3, 2, D, E, D, D, 1, 1),
             ('Grass / хард', 2, 0, D, E, None, None, 0, 0),
             ('grass', 5, 4, D, E, D, E, 0, 0),
             ('Unknown cup', 3, 2, D, E, D, E, 0, 0),
             ('=HYPERLINK("x") hard', 1, 1, D, E, D, E, 0, 0)]
    return {'session': [('on', 'repeatable read', NOW)],
            'schema': list(a.EXPECTED.items()), 'totals': [(21, 14, 7, 6)], 'names': names}


class Cursor:
    def __init__(self, connection, named):
        self.connection, self.named, self.offset = connection, named, 0
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, sql, params):
        assert sql in a.SQL.values() and sql.count('%s') == len(params)
        self.name = next(name for name, value in a.SQL.items() if value == sql)
        assert (self.name == 'names') == self.named
        self.connection.executed.append(self.name)
        self.offset = 0
        if self.connection.failure:
            raise self.connection.failure
    def fetchmany(self, n):
        result = self.connection.data[self.name][self.offset:self.offset + n]
        self.offset += len(result)
        return result


class Connection:
    def __init__(self, data=None, failure=None):
        self.data = fixtures() if data is None else data
        self.executed, self.failure = [], failure
        self.closed, self.rollbacks, self.session = False, 0, None
    def cursor(self, name=None, **kwargs):
        if name:
            assert kwargs == dict(scrollable=False, withhold=False)
        return Cursor(self, bool(name))
    def set_session(self, **kwargs):
        self.session = kwargs
    def rollback(self):
        self.rollbacks += 1
    def close(self):
        self.closed = True


class SurfaceAuditTests(unittest.TestCase):
    def test_russian_english_case_punctuation(self):
        for name, expected in [('ATP. хард, Финал', 'hard'), ('(HARD)', 'hard'),
                               ('ХаРд / Hard', 'hard'), ('грунт', 'clay'), ('CLAY!', 'clay'),
                               ('WTA — ТРАВА', 'grass'), ('[GrAsS]', 'grass')]:
            self.assertEqual(a.classify(name)[0], expected)
        self.assertEqual(a.classify('hard ХАРД hard')[1], ('hard', 'хард'))

    def test_unicode_word_boundaries(self):
        for name in ('hardcourt', 'clayish', 'grassland', 'суперхард', 'хардовый',
                     'трава123', '2hard', '_hard_', 'хард_корт', '硬hard',
                     'hardé', '\u0301hard', 'hard\u0301', 'грунтовый'):
            with self.subTest(name=name):
                self.assertEqual(a.classify(name), ('unknown', ()))
        self.assertEqual(a.classify('«ХАРД» — трава')[0], 'conflict')
        self.assertEqual(a.classify('ＨＡＲＤ')[0], 'hard')  # NFKC, not a city lookup.

    def test_unknown_conflict_empty_and_null(self):
        for name in (None, '', ' \t\n', 'Wimbledon London July', 'Paris France'):
            self.assertEqual(a.classify(name), ('unknown', ()))
        self.assertEqual(a.classify('hard clay')[0], 'conflict')
        self.assertEqual(a.classify('грунт + GRASS + хард')[0], 'conflict')
        with self.assertRaises(a.AuditFailure):
            a.classify(123)

    def test_full_counts_percentages_and_canonical_scope(self):
        connection = Connection()
        result = a.audit(connection)
        self.assertEqual(connection.executed, list(a.SQL))
        all_rows, canonical = (result['summary']['scopes'][scope] for scope in ('all', 'canonical'))
        self.assertEqual((all_rows['matches_total'], canonical['matches_total']), (21, 14))
        self.assertEqual((all_rows['unique_names'], canonical['unique_names']), (7, 6))
        self.assertEqual(all_rows['name_groups_including_null'], 8)
        self.assertEqual(all_rows['null_name_matches'], 2)
        self.assertEqual(all_rows['blank_name_matches'], 1)
        self.assertEqual({k:v['matches'] for k,v in all_rows['categories'].items()},
                         dict(hard=5, clay=3, grass=5, unknown=6, conflict=2))
        self.assertEqual(all_rows['automatically_classified_percent'], 61.904762)
        self.assertEqual(canonical['automatically_classified_percent'], 71.428571)
        self.assertEqual(all_rows['existing_surface_nonempty'], 1)
        for scope in ('all', 'canonical'):
            rows = list(csv.reader(io.StringIO(result['csv'][scope + '_tournaments.csv'])))
            self.assertEqual(rows[0], list(a.HEADER))
            for label in ('unknown', 'conflict'):
                subset = list(csv.reader(io.StringIO(result['csv'][scope + '_' + label + '.csv'])))[1:]
                self.assertEqual(subset, [row for row in rows[1:] if row[2] == label])

    def test_all_names_beyond_one_fetch_batch(self):
        data = fixtures()
        data['names'] = [(f'Event {i:04d} хард', 2, 1, D, E, D, D, 0, 0) for i in range(600)]
        data['totals'] = [(1200, 600, 600, 600)]
        result = a.audit(Connection(data))
        self.assertEqual(result['summary']['scopes']['all']['unique_names'], 600)
        self.assertEqual(result['summary']['scopes']['all']['matches_total'], 1200)
        self.assertEqual(len(list(csv.reader(io.StringIO(result['csv']['all_tournaments.csv'])))), 601)

    def test_no_limit_and_sql_safety(self):
        for sql in a.SQL.values():
            self.assertTrue(sql.lstrip().startswith('SELECT '))
            self.assertNotRegex(sql.upper(), r'\b(UPDATE|INSERT|DELETE|CREATE|DROP|CALL|COPY|LIMIT)\b')
        self.assertIn('public.matches', a.SQL['names'])
        self.assertIn('GROUP BY 1', a.SQL['names'])
        self.assertIn('duplicate_of IS NULL', a.SQL['names'])
        self.assertIn("t.typnamespace='pg_catalog'::pg_catalog.regnamespace", a.SQL['schema'])

    def test_safe_reversible_csv(self):
        names = [None, '', r'\N', r'\other', "'literal", '=SUM(1,2)', '+1', '-1', '@cmd',
                 '  =cmd', '＝cmd', '\t=cmd', '\r+cmd', '\n@cmd', '\ufeff=cmd', '\u200b=cmd',
                 'Турнир, "Хард"\nФинал', 'обычное название']
        for name in names:
            cell = a.csv_name(name)
            self.assertEqual(a.original_name(cell), name)
            self.assertFalse(a.formula_risk(cell))
            output = io.StringIO(newline='')
            csv.writer(output).writerow([cell])
            self.assertEqual(next(csv.reader(io.StringIO(output.getvalue()))), [cell])
        self.assertNotEqual(a.csv_name(None), a.csv_name(''))
        self.assertNotEqual(a.csv_name(None), a.csv_name(r'\N'))

    def test_empty_database(self):
        data = fixtures()
        data.update(totals=[(0, 0, 0, 0)], names=[])
        result = a.audit(Connection(data))
        self.assertTrue(result['complete'])
        for info in result['summary']['scopes'].values():
            self.assertEqual(info['automatically_classified_percent'], 0)
            self.assertEqual(info['unique_names'], 0)
        for text in result['csv'].values():
            self.assertEqual(list(csv.reader(io.StringIO(text))), [list(a.HEADER)])

    def test_limits_and_no_partial_success(self):
        with patch.object(a, 'MAX_NAME_BYTES', 2), self.assertRaises(a.AuditFailure):
            a.audit(Connection())
        with patch.object(a, 'MAX_CSV_BYTES', 1000), self.assertRaises(a.AuditFailure):
            a.audit(Connection())
        with patch.object(a, 'MAX_EXPORT_BYTES', 10), self.assertRaises(a.AuditFailure):
            a.serialize(a.audit(Connection()))
        data = fixtures()
        data['names'] = data['names'][:-1]
        with self.assertRaises(a.AuditFailure):
            a.audit(Connection(data))  # Missing group must not pass totals check.
        with self.assertRaises(a.AuditFailure):
            a.expired(None, None)

    def test_schema_readonly_types_and_dates(self):
        for key, value in [('schema', []), ('session', [('off', 'repeatable read', NOW)]),
                           ('totals', [(21, 99, 7, 6)])]:
            data = fixtures()
            data[key] = value
            with self.assertRaises(a.AuditFailure):
                a.audit(Connection(data))
        for value in (True, -1, SECRET):
            with self.assertRaises(a.AuditFailure):
                a.count(value)
        for value in (SECRET, NOW, dt.date.max):
            with self.assertRaises(a.AuditFailure):
                a.date_text(value)

    def test_materialize_complete_export(self):
        result = a.audit(Connection())
        with tempfile.TemporaryDirectory() as root:
            source, dest = Path(root) / 'input.json', Path(root) / 'report'
            source.write_text(a.serialize(result), encoding='utf-8')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(a.unpack(source, dest), 0)
            self.assertEqual(set(p.name for p in dest.iterdir()), {*a.FILES, 'summary.json'})
            self.assertNotIn('ATP', out.getvalue())
            self.assertNotIn('HYPERLINK', out.getvalue())
            self.assertEqual(json.loads((dest / 'summary.json').read_text()), result['summary'])

    def test_materialize_rejects_partial_tampered_or_unsafe_exports(self):
        result = a.audit(Connection())
        variants = []
        variant = json.loads(a.serialize(result));variant['complete'] = False;variants.append(variant)
        variant = json.loads(a.serialize(result));variant['csv']['../unexpected'] = SECRET;variants.append(variant)
        variant = json.loads(a.serialize(result));variant['summary']['extra'] = SECRET;variants.append(variant)
        variant = json.loads(a.serialize(result));variant['summary']['scopes']['all']['matches_total'] = 1;variants.append(variant)
        variant = json.loads(a.serialize(result));variant['csv']['all_unknown.csv'] = variant['csv']['all_unknown.csv'].splitlines()[0] + '\n';variants.append(variant)
        variant = json.loads(a.serialize(result));variant['csv']['all_tournaments.csv'] = variant['csv']['all_tournaments.csv'].replace("'=HYPERLINK", '=HYPERLINK');variants.append(variant)
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'input.json'
            for i, variant in enumerate(variants):
                source.write_text(json.dumps(variant), encoding='utf-8')
                out = io.StringIO()
                dest = Path(root) / str(i)
                with contextlib.redirect_stdout(out):
                    self.assertEqual(a.unpack(source, dest), 1)
                self.assertFalse(dest.exists())
                self.assertNotIn(SECRET, out.getvalue())

    def test_sql_only_surface_metric_bounds_and_provenance(self):
        original = a.audit(Connection())
        for info in original['summary']['scopes'].values():
            self.assertEqual(info['existing_surface_nonempty_source'], 'sql_only_not_recomputed_from_csv')
        invalid = [(-1, 0), (1, -1), (22, 1), (21, 15), (0, 1), (2, 3),
                   (True, 0), (1.0, 0), ('1', 0)]
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'input.json'
            for i, (all_value, canonical_value) in enumerate(invalid):
                variant = json.loads(a.serialize(original))
                scopes = variant['summary']['scopes']
                scopes['all']['existing_surface_nonempty'] = all_value
                scopes['canonical']['existing_surface_nonempty'] = canonical_value
                source.write_text(a.serialize(variant), encoding='utf-8')
                destination = Path(root) / ('invalid-' + str(i))
                with self.subTest(all=all_value, canonical=canonical_value), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(a.unpack(source, destination), 1)
                self.assertFalse(destination.exists())
                self.assertEqual(output.getvalue().strip(), 'SURFACE_AUDIT_ARTIFACT_FAILED')
            for i, label in enumerate(('csv_verified', None)):
                variant = json.loads(a.serialize(original))
                info = variant['summary']['scopes']['all']
                if label is None:
                    del info['existing_surface_nonempty_source']
                else:
                    info['existing_surface_nonempty_source'] = label
                source.write_text(a.serialize(variant), encoding='utf-8')
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(a.unpack(source, Path(root) / ('provenance-' + str(i))), 1)

    def test_sql_only_metric_is_not_claimed_as_csv_recomputed(self):
        report = a.audit(Connection())
        report['summary']['scopes']['all']['existing_surface_nonempty'] = 5
        report['summary']['scopes']['canonical']['existing_surface_nonempty'] = 3
        # CSV cannot confirm these plausible SQL-only values; acceptance must
        # explicitly retain the non-verifiable provenance, not claim a rebuild.
        with tempfile.TemporaryDirectory() as root:
            source, dest = Path(root) / 'input.json', Path(root) / 'report'
            source.write_text(a.serialize(report), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(a.unpack(source, dest), 0)
            scopes = json.loads((dest / 'summary.json').read_text())['scopes']
            self.assertEqual(scopes['all']['existing_surface_nonempty'], 5)
            self.assertEqual(scopes['canonical']['existing_surface_nonempty'], 3)
            for info in scopes.values():
                self.assertEqual(info['existing_surface_nonempty_source'], 'sql_only_not_recomputed_from_csv')
            for name, text in report['csv'].items():
                self.assertEqual((dest / name).read_text(), text)

    def test_remote_error_sanitization_cleanup_and_options(self):
        driver = types.ModuleType('psycopg2')
        config = {key:'literal' for key in a.CONNECTION_FIELDS}
        config.update(POSTGRES_PORT='5432', POSTGRES_PASSWORD=SECRET)
        for pgcode, expected in [('57014', 'query_timeout'), ('55P03', 'lock_timeout'),
                                 ('42P01', 'table_unavailable'), ('42703', 'schema_mismatch'),
                                 ('28000', 'connection_or_query_failed')]:
            error = RuntimeError(SECRET);error.pgcode = pgcode
            connection = Connection(failure=error)
            def connect(**kwargs):
                self.assertEqual(kwargs['connect_timeout'], 5)
                for setting in ('default_transaction_read_only=on', 'statement_timeout=5000', 'lock_timeout=1000'):
                    self.assertIn(setting, kwargs['options'])
                return connection
            driver.connect = connect
            out = io.StringIO()
            with patch.dict(sys.modules, {'psycopg2':driver}), \
                 patch.object(a.importlib.util,'find_spec',return_value=object()), \
                 patch.object(a,'connection_values',return_value=(config,5432)), \
                 patch.object(a.signal,'signal'),patch.object(a.signal,'alarm'), \
                 contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):
                self.assertEqual(a.remote(),1)
            self.assertEqual(json.loads(out.getvalue()),dict(complete=False,audit_status=expected))
            self.assertNotIn(SECRET,out.getvalue())
            self.assertTrue(connection.closed and connection.rollbacks)
            self.assertEqual(connection.session,dict(readonly=True,isolation_level='REPEATABLE READ',autocommit=False))
        with patch.object(a.importlib.util,'find_spec',return_value=None), \
             patch.object(a.signal,'signal'),patch.object(a.signal,'alarm'), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(a.remote(),1)
        self.assertEqual(json.loads(out.getvalue())['audit_status'],'dependency_unavailable')


if __name__ == '__main__':
    unittest.main()
