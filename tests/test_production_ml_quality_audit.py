"""Offline tests: no PostgreSQL or S3 access."""
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch, mock_open

P = Path(__file__).resolve().parents[1] / 'infra/audit/production_ml_quality_audit.py'
spec = importlib.util.spec_from_file_location('ml_audit', P)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

class Cursor:
    def __init__(self, responses):
        self.responses = responses
        self.sql = []
        self.index = 0
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params):
        self.sql.append((sql, params))
    def fetchmany(self, n):
        rows = self.responses[self.index]
        self.index += 1
        return rows[:n]

class Connection:
    def __init__(self, responses):
        self.c = Cursor(responses)
    def cursor(self): return self.c

def fixture():
    totals=[0]*len(a.TOTAL_NAMES)
    categories=[0]*len(a.CATEGORY_NAMES)
    return Connection([
        [('on','repeatable read',dt.datetime(2026,11,12))],
        list(a.EXPECTED.items()),
        [tuple(totals)],
        [tuple(categories)],
        []
    ])

class QualityAuditTests(unittest.TestCase):
    def test_empty_database(self):
        conn=fixture()
        report=a.audit(conn)
        self.assertTrue(report['complete'])
        self.assertEqual(report['totals']['matches_total'],0)
        self.assertEqual(report['years'],[])
        self.assertEqual(report['training_readiness']['status'],'unknown')
        self.assertEqual(len(conn.c.sql),5)
        self.assertTrue(all('SELECT' in sql for sql,_ in conn.c.sql))

    def test_year_aggregates(self):
        conn=fixture()
        conn.c.responses[-1]=[(2025,100,98,90,88,50),(2026,40,39,30,28,20)]
        conn.c.responses[2]=[tuple([140]+[0]*(len(a.TOTAL_NAMES)-1))]
        report=a.audit(conn)
        self.assertEqual([x['matches'] for x in report['years']],[100,40])
        self.assertEqual(report['years'][0]['finished_canonical'],90)

    def test_readonly_mandatory(self):
        conn=fixture()
        conn.c.responses[0]=[('off','read committed',dt.datetime(2026,11,12))]
        with self.assertRaises(a.AuditFailure): a.audit(conn)

    def test_schema_mismatch(self):
        conn=fixture()
        conn.c.responses[1]=[]
        with self.assertRaises(a.AuditFailure): a.audit(conn)

    def test_row_limits(self):
        conn=fixture()
        conn.c.responses[-1]=[(2000+i,1,1,1,1,1) for i in range(41)]
        with self.assertRaises(a.AuditFailure): a.audit(conn)

    def test_rejects_invalid_count(self):
        for bad in (-1,True,1.5,None):
            with self.subTest(bad=bad),self.assertRaises(a.AuditFailure): a.count(bad)

    def test_output_bounded(self):
        with self.assertRaises(a.AuditFailure):
            a.serialize({'large':'x'*20000})

    def test_no_raw_identifiers(self):
        report=a.audit(fixture())
        data=a.serialize(report)
        for forbidden in ('player1','player2','pbp_s3_path','POSTGRES_PASSWORD','s3://'):
            self.assertNotIn(forbidden,data)

    def test_config_validation(self):
        with patch.object(a.Path,'read_bytes',return_value=b'POSTGRES_PORT=not-a-port\n'):
            with self.assertRaises(a.AuditFailure): a.settings()

    def test_no_placeholder_query(self):
        self.assertIn('COUNT(*) FILTER', a.QUERIES['categories'])
        self.assertNotIn('CASE WHEN %s = %s', a.QUERIES['categories'])

    def test_malformed_quoted_config_rejected(self):
        values = b'POSTGRES_HOST="unterminated\\n'
        with patch.object(a.Path,'open',mock_open(read_data=values)):
            with self.assertRaises(a.AuditFailure): a.settings()

    def test_fixed_sql_allowlist(self):
        self.assertEqual(set(a.QUERIES),{'session','schema','totals','years','categories'})
        for sql in a.QUERIES.values():
            self.assertNotRegex(sql,r'(?i)\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE)\b')

if __name__=='__main__':
    unittest.main()
