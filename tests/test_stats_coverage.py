"""Fabricated fixtures; coverage is independent of PBP. No source network access."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
def module(name):
    s=importlib.util.spec_from_file_location(name,ROOT/"infra/audit"/(name+".py"))
    m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
c=module("stats_coverage_core")
r=module("production_stats_coverage")
A="00000000-0000-4000-8000-000000000001"
B="00000000-0000-4000-8000-000000000002"
K="nbbet_stats/gender=F/level=tour/year=2020/part-details.parquet"
D="nbbet_details/gender=F/level=tour/year=2020/part-details.parquet"
X="nbbet_stats/gender=F/level=tour/year=2020/part-new-crosscheck-1.parquet"

class Coverage(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=c.Store(Path(self.tmp.name)/"audit.sqlite")
        self.s.add_matches([(A,249197,2020,"F","tour","hard","Fixture",3,1,"2026-01-01",3,0),
                            (B,26041,2020,"F","tour",None,"Fixture",9,1,None,1,0)])
        self.s.set("pg_snapshot","2026-01-01T00:00:00Z"); self.s.db.commit()
    def tearDown(self): self.s.db.close(); self.tmp.cleanup()
    def inventory(self,keys=(K,D)):
        self.s.inventory_start()
        for key in keys: self.s.inventory_add(key,"etag",100)
        self.s.inventory_finish(["nbbet_stats","nbbet_details"])
        self.s.set("inventory_stable",True); self.s.db.commit()
    def row(self,scope=0,ts="2026-01-01",mid=A,**kw):
        return {"canonical_match_id":mid,"scope":scope,"fetched_at":ts,
                **dict.fromkeys(c.FIELDS,0),**kw}
    def report(self): return c.summarize(self.s)
    def test_absent_pbp_sibling_stats_elsewhere_and_no_pbp_flag(self):
        self.inventory()
        self.s.ingest(K,[self.row(),self.row(1),self.row(2)])
        self.s.ingest(D,[{"canonical_match_id":A,"fetched_at":"2026-01-01","sets_json":"[{},{}]"}])
        a=self.report()["controls"][0]
        self.assertEqual(a["category"],"full")
        self.assertFalse(a["pbp_pg_flag"])
        self.assertEqual(a["physical_presence"]["pbp"],"unknown_not_fully_scanned")
    def test_latest_crosscheck_not_double_counted_and_zero_valid(self):
        self.inventory((K,X))
        self.s.ingest(K,[self.row(aces_p1=None)])
        self.s.ingest(X,[self.row(ts="2026-02-01",aces_p1=0)])
        v=self.s.latest(A,"nbbet_stats")[0]
        self.assertEqual(v["payload"]["aces_p1"],0)
        g=next(g for g in self.report()["groups"] if g["group"]==("overall",3))
        self.assertEqual(g["denominator"],1); self.assertEqual(g["fields_match"]["aces_p1"],1)
        self.assertEqual(g["fields_match_percent_all_matches"]["aces_p1"],100)
    def test_same_time_conflict_unknown_and_order_independent(self):
        self.inventory((K,X))
        self.s.ingest(K,[self.row(aces_p1=1)])
        self.s.ingest(X,[self.row(aces_p1=2)])
        self.assertTrue(self.s.latest(A,"nbbet_stats")[0]["conflict"])
        self.assertEqual(self.report()["controls"][0]["category"],"unknown")
    def test_missing_and_incomplete_not_conflated(self):
        self.inventory((K,))
        self.s.fail(K,"permission_denied")
        self.assertTrue(all(x["category"]=="unknown" for x in self.report()["controls"]))
        self.s.ingest(K,[])
        self.assertTrue(all(x["category"]=="absent" for x in self.report()["controls"]))
    def test_any_unread_partition_taints_all_stats(self):
        other="nbbet_stats/gender=M/level=challenger/year=2026/part-x.parquet"
        self.inventory((K,other)); self.s.ingest(K,[self.row()])
        self.assertFalse(self.report()["controls"][0]["latest_proven"])
    def test_inventory_changed_means_unknown_not_complete(self):
        self.inventory((K,)); self.s.ingest(K,[])
        self.s.set("inventory_stable",False)
        self.assertFalse(self.report()["complete"])
        self.assertEqual(self.report()["controls"][0]["category"],"unknown")
    def test_rollback_partial_object_no_checkpoint_false_success(self):
        self.inventory((K,))
        def broken():
            yield self.row()
            raise OSError("private failure")
        with self.assertRaises(OSError): self.s.ingest(K,broken())
        self.s.fail(K,"read_or_schema_failed")
        self.assertEqual(self.s.latest(A,"nbbet_stats"),{})
        self.assertEqual(self.report()["controls"][0]["category"],"unknown")
    def test_resume_skips_completed_and_changed_etag_invalidates(self):
        self.inventory((K,)); self.s.ingest(K,[self.row()])
        self.inventory((K,))
        self.assertEqual(self.s.db.execute("SELECT state FROM objects").fetchone()[0],"done")
        self.s.inventory_start(); self.s.inventory_add(K,"different",100)
        self.s.inventory_finish(["nbbet_stats"])
        self.assertEqual(self.s.latest(A,"nbbet_stats"),{})
        self.assertEqual(self.s.db.execute("SELECT state FROM objects").fetchone()[0],"pending")
    def test_missing_details_keeps_expected_sets_unknown(self):
        self.inventory((K,D)); self.s.ingest(K,[self.row(),self.row(1)])
        self.s.ingest(D,[])
        a=self.report()["controls"][0]
        self.assertIsNone(a["expected_sets"]); self.assertEqual(a["category"],"partial")
    def test_optional_metrics_do_not_define_core_complete(self):
        self.inventory((K,D))
        rows=[self.row(scope=i,**{f:None for f in c.FIELDS if f not in c.CORE}) for i in (0,1)]
        self.s.ingest(K,rows)
        self.s.ingest(D,[{"canonical_match_id":A,"sets_json":"[{}]"}])
        a=self.report()["controls"][0]
        self.assertEqual(a["category"],"full"); self.assertFalse(a["strict28_scope0"])
    def test_status9_separate_and_fields_denominators_explicit(self):
        self.inventory((K,)); self.s.ingest(K,[self.row(mid=B)])
        groups=self.report()["groups"]
        self.assertEqual({g["group"][1] for g in groups if g["group"][0]=="overall"},{3,9})
        g=next(g for g in groups if g["group"]==("overall",9))
        self.assertIsNone(g["fields_set_percent_expected_scopes"])
    def test_missing_scope_rejected(self):
        self.inventory((K,))
        row=self.row(); del row["scope"]
        with self.assertRaises(ValueError): self.s.ingest(K,[row])
    def test_unmatched_ids_counted(self):
        self.inventory((K,))
        self.s.ingest(K,[self.row(mid="unknown-fixture")])
        self.assertEqual(self.report()["unmatched_distinct_ids"],1)
    def test_reports_only_aggregates_and_control_ids(self):
        self.inventory((K,)); self.s.ingest(K,[])
        out=Path(self.tmp.name)/"report"; c.write_reports(self.report(),out)
        self.assertEqual({p.name for p in out.iterdir()},{"README.md","coverage.csv","coverage.json","fields.csv"})
        text=(out/"coverage.json").read_text()
        self.assertNotIn("password",text); self.assertNotIn("raw_json",text)
    def test_partition_scope_and_itf_exclusion(self):
        self.assertIsNone(c.partition("nbbet_stats/gender=M/level=itf/year=2020/part-a.parquet"))
        self.assertIsNone(c.partition("nbbet_stats/gender=M/level=tour/year=2015/part-a.parquet"))
        self.assertIsNotNone(c.partition(X))
    def test_monitor_threshold_read_as_data_not_import(self):
        p=Path(self.tmp.name)/"health.py"
        p.write_text("MIN_FREE_DISK_PERCENT=15.0\nraise RuntimeError('must not execute')")
        self.assertEqual(r.read_constant(p,"MIN_FREE_DISK_PERCENT"),15)
    def test_error_redaction(self):
        self.assertEqual(r.safe_code(Exception("password=private")),"read_or_schema_failed")
        self.assertNotIn("private",r.safe_code(OSError("private")))
    def test_missing_stamp_oldest_under_source_contract(self):
        self.inventory((K,X)); self.s.ingest(K,[self.row(ts=None)])
        self.s.ingest(X,[self.row(ts="2026-02-01",aces_p1=7)])
        self.assertEqual(self.s.latest(A,"nbbet_stats")[0]["payload"]["aces_p1"],7)



class Adapter(unittest.TestCase):
    def test_complete_listing_has_no_pbp_dependency(self):
        class Client:
            calls=[]
            def list_objects_v2(self,**kwargs):
                self.calls.append(kwargs)
                return {"Contents":[{"Key":K,"ETag":"one","Size":100}]} if kwargs["Prefix"]=="nbbet_stats/gender=F/level=tour/" else {}
        class Guard:
            def check(self,**kwargs): pass
        with tempfile.TemporaryDirectory() as t:
            s=c.Store(Path(t)/"audit.sqlite"); client=Client()
            first=r.inventory(s,client,"fixture",["nbbet_stats"],Guard())
            s.inventory_finish(["nbbet_stats"])
            self.assertEqual(len(client.calls),4)
            self.assertEqual(s.db.execute("SELECT COUNT(*) FROM objects").fetchone()[0],1)
            second=r.inventory(s,client,"fixture",["nbbet_stats"],Guard())
            self.assertEqual(first,second); s.db.close()
    def test_listing_error_never_marks_complete(self):
        class Client:
            def list_objects_v2(self,**kwargs): raise PermissionError("secret")
        class Guard:
            def check(self,**kwargs): pass
        with tempfile.TemporaryDirectory() as t:
            s=c.Store(Path(t)/"audit.sqlite")
            with self.assertRaises(PermissionError): r.inventory(s,Client(),"fixture",["nbbet_stats"],Guard())
            self.assertFalse(s.get("inventory_complete",False)); s.db.close()
    def test_group_stream_projection_and_exact_get_count(self):
        try: import pyarrow as pa; import pyarrow.parquet as pq
        except ImportError: self.skipTest("Parquet integration runs in dependency CI stage")
        from unittest.mock import patch
        import io
        buf=pa.BufferOutputStream()
        pq.write_table(pa.Table.from_pylist([{"canonical_match_id":A,"scope":0,"aces_p1":0,
                                            "raw_json":"excluded private payload"}]),buf,row_group_size=1)
        data=buf.getvalue().to_pybytes()
        class Client:
            calls=0
            def get_object(self,**kwargs):
                self.calls+=1
                if kwargs["IfMatch"]!="fixture": raise AssertionError("conditional read missing")
                return {"Body":io.BytesIO(data),"ContentLength":len(data),"ETag":"fixture"}
        class Guard:
            decode_cap=1024**2
            def check(self,**kwargs): pass
        client=Client()
        with tempfile.TemporaryDirectory() as t, patch.object(r.time,"sleep"):
            values=list(r.rows(client,"fixture",(K,"fixture",len(data),"nbbet_stats"),Guard(),t,{}))
        self.assertEqual(client.calls,1)
        self.assertEqual(values[0]["aces_p1"],0)
        self.assertNotIn("raw_json",values[0])
    def test_bad_timestamp_and_schema_are_unknown_not_empty(self):
        with self.assertRaises(ValueError): c.stamp("not a timestamp")
        with self.assertRaises(ValueError): c.metric("private string")
        self.assertIsNone(c.metric(float("nan")))
        self.assertEqual(c.metric(0),0)
    def test_cpu_memory_pressure_pauses_then_stops(self):
        from unittest.mock import patch
        fake={"MemTotal":4*1024**3,"MemAvailable":2*1024**3}
        with tempfile.TemporaryDirectory() as t, patch.object(r,"meminfo",return_value=fake):
            g=r.Guard(t,15)
            with patch.object(g,"sample",return_value=({"MemAvailable":0},r.shutil.disk_usage(t),1)),patch.object(r.time,"sleep") as sleep:
                with self.assertRaises(r.Stop): g.check()
                self.assertGreater(sleep.call_count,0)
    def test_launcher_and_snapshot_contract(self):
        source=(ROOT/"infra/audit/production_stats_coverage.py").read_text()
        self.assertIn('readonly=True,isolation_level="REPEATABLE READ"',source)
        self.assertIn('duplicate_of IS NULL AND status IN (3,9)',source)
        self.assertNotIn("fetch_match_page",source)
        self.assertNotIn("put_object",source)
        self.assertIn('"total_max_attempts":1',source)
        self.assertIn('IfMatch=etag',source)



class PostgreSQLSnapshot(unittest.TestCase):
    def test_actual_pg16_readonly_snapshot_and_control_denominator(self):
        import os
        if os.environ.get("COVERAGE_TEST_POSTGRES")!="1": self.skipTest("isolated PG16 CI service only")
        import psycopg2
        from types import SimpleNamespace
        config=dict(host="127.0.0.1",port=5432,user="postgres",password="offline-fixture-password",dbname="postgres")
        conn=psycopg2.connect(**config); conn.autocommit=True
        try:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS public.matches (
                    match_id uuid PRIMARY KEY, nbbet_match_id int, match_date date,
                    gender text, level text, surface text, tournament_name text,
                    status int, duplicate_of uuid,details_fetched_at timestamp,
                    details_version int,has_pbp bool)""")
                cur.execute("TRUNCATE public.matches")
                cur.execute("INSERT INTO public.matches VALUES (%s,249197,'2020-01-15','F','tour','hard','Fixture',3,NULL,NULL,1,false)",(A,))
                cur.execute("INSERT INTO public.matches VALUES (%s,453599,'2020-01-15','M','tour',NULL,'Fixture',8,NULL,NULL,1,false)",(B,))
            sconfig=SimpleNamespace(**{"postgres_"+k:v for k,v in
                (("host",config["host"]),("port",5432),("user","postgres"),("password",config["password"]),("db","postgres"))})
            class Guard:
                def check(self,**kwargs): pass
            with tempfile.TemporaryDirectory() as temp:
                state=c.Store(Path(temp)/"pg.sqlite")
                r.snapshot(state,sconfig,Guard())
                rows=state.db.execute("SELECT nbid,eligible,pbp_flag FROM matches ORDER BY nbid").fetchall()
                self.assertEqual(rows,[(249197,1,0),(453599,0,0)])
                self.assertIsNotNone(state.get("pg_snapshot"))
                state.db.close()
        finally: conn.close()



class OlderPartition(unittest.TestCase):
    def test_pre2016_files_do_not_invalidate_requested_scope(self):
        class Client:
            def list_objects_v2(self,**kwargs):
                return {"Contents":[{"Key":"nbbet_stats/gender=F/level=tour/year=2015/part-a.parquet","ETag":"fixture","Size":100}]}
        class Guard:
            def check(self,**kw): pass
        with tempfile.TemporaryDirectory() as temp:
            state=c.Store(Path(temp)/"state.sqlite")
            r.inventory(state,Client(),"fixture",["nbbet_stats"],Guard())
            state.inventory_finish(["nbbet_stats"])
            self.assertEqual(state.db.execute("SELECT COUNT(*) FROM objects").fetchone()[0],0)
            state.db.close()

if __name__=="__main__": unittest.main()
