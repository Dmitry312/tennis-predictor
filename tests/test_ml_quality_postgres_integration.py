"""Integration tests against an ephemeral, disposable PostgreSQL database.

Never connect to production: the test accepts only loopback host and a fixed
test database name. Workflow provisions an isolated PostgreSQL service.
"""
import datetime as dt
import os
import psycopg2
import unittest

from test_production_ml_quality_audit import a

DBNAME = 'ml_audit_test'

class PostgreSQLIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        host = os.environ.get('ML_AUDIT_TEST_HOST', '127.0.0.1')
        if host not in ('127.0.0.1', 'localhost'):
            raise RuntimeError('integration tests require loopback PostgreSQL')
        cls.conn = psycopg2.connect(
            host=host, port=5432, dbname=DBNAME,
            user='ml_audit_test', password='test_only_password',
            connect_timeout=3)
        cls.conn.autocommit = True
        with cls.conn.cursor() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS public.matches (
              match_id uuid PRIMARY KEY, match_date date,
              player1 varchar, player2 varchar, gender varchar, level varchar,
              surface varchar, status int2, duplicate_of uuid, has_pbp bool,
              pbp_s3_path varchar, pbp_completeness float8,
              odds_open_p1 numeric, odds_open_p2 numeric,
              odds_open_captured_at timestamp, odds_open_source varchar,
              odds_open_status int2, start_ts timestamp)""")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        with self.conn.cursor() as c:
            c.execute('TRUNCATE public.matches')

    def run_audit(self):
        self.conn.autocommit = False
        with self.conn.cursor() as c:
            c.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        try:
            return a.audit(self.conn)
        finally:
            self.conn.rollback()
            self.conn.autocommit = True

    def test_empty_schema_and_queries(self):
        report = self.run_audit()
        self.assertEqual(report['totals']['matches_total'], 0)
        self.assertEqual(report['years'], [])

    def test_seeded_invalid_and_valid_rows(self):
        with self.conn.cursor() as c:
            c.execute("""INSERT INTO public.matches
            (match_id,match_date,player1,player2,gender,level,surface,status,
             has_pbp,pbp_s3_path,pbp_completeness,odds_open_p1,odds_open_p2,
             odds_open_captured_at,odds_open_source,odds_open_status,start_ts)
            VALUES
            ('00000000-0000-0000-0000-000000000001','2025-03-01',
             'A','B','M','tour','hard',3,true,'pbp/file',0.9,1.8,2.1,
             '2025-02-28 12:00:00','listing',1,'2025-03-01 12:00:00'),
            ('00000000-0000-0000-0000-000000000002','2025-03-02',
             'C','C','F','itf',NULL,3,false,NULL,NULL,2.0,2.0,
             '2025-03-03 12:00:00','page',3,'2025-03-02 12:00:00')""")
        report = self.run_audit()
        t = report['totals']
        self.assertEqual(t['matches_total'],2)
        self.assertEqual(t['same_player'],1)
        self.assertEqual(t['has_pbp'],1)
        self.assertEqual(t['odds_at_or_after_start'],1)
        self.assertEqual(t['strict_prematch_odds_finished_canonical'],1)
        self.assertEqual(report['years'][0]['matches'],2)
        self.assertEqual(report['categories']['odds_capture_live_or_final'],1)

    def test_dangling_duplicate_and_missing_odds(self):
        with self.conn.cursor() as c:
            c.execute("""INSERT INTO public.matches
            (match_id, match_date, duplicate_of, odds_open_p1)
            VALUES ('00000000-0000-0000-0000-000000000003','2024-01-01',
            '00000000-0000-0000-0000-000000000004',1.5)""")
        report = self.run_audit()
        self.assertEqual(report['totals']['dangling_duplicate'],1)
        self.assertEqual(report['totals']['one_sided_odds'],1)
        self.assertEqual(report['totals']['marked_duplicates'],1)

if __name__ == '__main__':
    unittest.main()
