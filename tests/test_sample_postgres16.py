"""Actual PostgreSQL 16 planner regression, using an isolated CI fixture database only."""
import os
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import acquire_match_samples as a

pytestmark=pytest.mark.skipif(os.environ.get("SAMPLE_TEST_POSTGRES")!="1",
                            reason="isolated PostgreSQL CI fixture not enabled")


def nodes(plan):
    yield plan
    for child in plan.get("Plans",[]): yield from nodes(child)


def test_uuid_alias_sort_reproduces_failure_and_qualified_order_uses_pk():
    import psycopg2
    connection=psycopg2.connect(connect_timeout=3)
    try:
        connection.autocommit=True
        with connection.cursor() as c:
            c.execute("CREATE TABLE public.matches (match_id uuid PRIMARY KEY, gender text, level text, "
                      "match_date date, surface text, status integer, details_version integer, "
                      "has_pbp boolean, pbp_s3_path text, duplicate_of uuid)")
            c.execute("INSERT INTO public.matches SELECT md5(i::text)::uuid,'M','tour',"
                      "DATE '2025-01-01','hard',3,3,false,NULL,NULL FROM generate_series(1,5000) s(i)")
            c.execute("ANALYZE public.matches")
        connection.autocommit=False
        connection.set_session(readonly=True,isolation_level="REPEATABLE READ")
        with connection.cursor() as c:
            c.execute("SET LOCAL statement_timeout=5000")
            # This is the exact faulty ordering shape: output alias resolves to text.
            c.execute("EXPLAIN (FORMAT JSON) SELECT match_id::text,gender,level,match_date::text,"
                      "left(surface,64),status,details_version,has_pbp,left(pbp_s3_path,1025),duplicate_of::text "
                      "FROM public.matches ORDER BY match_id LIMIT 129")
            original=c.fetchone()[0]
        assert any(node["Node Type"]=="Sort" for node in nodes(original[0]["Plan"]))
        with pytest.raises(ValueError,match="candidate_plan_unbounded"):
            a.check_candidate_plan(original)
        pool,report=a.candidate_pool(connection,pages=1)
        assert len(pool)==128 and report["rows_examined"]==128
        assert len(a.choose(pool,report)["selected_ids"])==20
        assert [row["match_id"] for row in pool]==sorted(row["match_id"] for row in pool)
    finally:
        connection.rollback()
        connection.close()
