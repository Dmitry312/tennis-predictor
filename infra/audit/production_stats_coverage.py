"""Read-only production adapter. No live NBBet calls, S3/PG writes or crosscheck imports.
Writes only an explicitly chosen private local checkpoint/report directory.
"""
import argparse
import ast
from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("stats_coverage_core",HERE/"stats_coverage_core.py")
core=importlib.util.module_from_spec(spec); spec.loader.exec_module(core)

class Stop(RuntimeError): pass

def safe_code(exc):
    if isinstance(exc,Stop): return str(exc) if str(exc) in ("resource_pressure","disk_guard","memory_guard","deadline","object_changed","inventory_unrecognized","runtime_policy_unverified","object_decode_guard") else "audit_stopped"
    return {"57014":"query_timeout","42501":"permission_denied"}.get(getattr(exc,"pgcode",None),"read_or_schema_failed")

def read_constant(path,name):
    # Parse exact production file as data; never execute monitoring code.
    raw=path.read_bytes()
    if len(raw)>1024*1024: raise Stop("runtime_policy_unverified")
    tree=ast.parse(raw)
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets):
            value=ast.literal_eval(node.value)
            if type(value)in (int,float) and 0<float(value)<100: return float(value)
    raise Stop("runtime_policy_unverified")

def meminfo():
    values={}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k,v=line.split(":",1); values[k]=int(v.strip().split()[0])*1024
    return values

class Guard:
    def __init__(self,root,disk_min,seconds=19800):
        self.root=root; self.disk_min=disk_min; self.end=time.monotonic()+seconds
        self.started=time.monotonic(); self.cpu=time.process_time()
        self.paused=0.; self.max_rss=0; self.min_available=None
        self.initial=meminfo()
        self.reserve=max(512*1024**2,int(self.initial["MemTotal"]*.25))
        self.decode_cap=min(128*1024**2,self.initial["MemAvailable"]//8)
        if self.decode_cap<16*1024**2: raise Stop("memory_guard")
    def sample(self):
        m=meminfo(); d=shutil.disk_usage(self.root)
        rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
        self.max_rss=max(self.max_rss,rss)
        self.min_available=min(self.min_available or m["MemAvailable"],m["MemAvailable"])
        return m,d,rss
    def check(self,extra_disk=0):
        if time.monotonic()>self.end: raise Stop("deadline")
        m,d,rss=self.sample()
        if d.free-extra_disk<max(d.total*(self.disk_min+5)/100,512*1024**2): raise Stop("disk_guard")
        if rss>min(768*1024**2,self.initial["MemTotal"]*.2): raise Stop("memory_guard")
        # Leave monitoring headroom. Never acquire the crosscheck lock.
        paused=0.
        while m["MemAvailable"]<self.reserve or os.getloadavg()[0]>max(1,os.cpu_count() or 1)*.7:
            if paused>=120 or time.monotonic()>self.end: raise Stop("resource_pressure")
            time.sleep(5); paused+=5; self.paused+=5
            m,d,rss=self.sample()
        elapsed=time.monotonic()-self.started
        cpu_used=time.process_time()-self.cpu
        duty_wait=max(0,cpu_used/.10-elapsed)
        if duty_wait:
            time.sleep(min(duty_wait,5)); self.paused+=min(duty_wait,5)
    def report(self):
        return {"elapsed_seconds":round(time.monotonic()-self.started,3),
                "cpu_seconds":round(time.process_time()-self.cpu,3),
                "max_rss_bytes":self.max_rss,"minimum_mem_available_bytes":self.min_available,
                "paused_seconds":self.paused,"decode_byte_guard":self.decode_cap,
                "disk_alert_percent":self.disk_min,"disk_stop_percent":self.disk_min+5,
                "cpu_duty_target":.1,"load_pause_per_cpu":.7}

def preflight(project,work):
    # Runtime thresholds and schedules, not assumptions about checkout freshness.
    health=project/"src/tennis_predictor/ingestion/health.py"
    disk_min=read_constant(health,"MIN_FREE_DISK_PERCENT")
    result={"checked_at":datetime.now(timezone.utc).isoformat(),
            "health_source_sha256":hashlib.sha256(health.read_bytes()).hexdigest(),
            "disk_alert_percent":disk_min,"cpu_ram_monitor_thresholds":"not_declared_in_health_py",
            "os_cpu_count":os.cpu_count(),"initial_load":os.getloadavg(),
            "initial_memory":{k:v for k,v in meminfo().items() if k in ("MemTotal","MemAvailable")},
            "disk":dict(zip(("total","used","free"),shutil.disk_usage(work))),
            "root_runtime":os.geteuid()==0}
    # Do not expose command lines/configs/logs. Report only known service states
    # and private file hashes. No start/stop/reload/lock of production services.
    result["services"]={}
    for service in ("tennis-stats-history.service","cron.service"):
        r=subprocess.run(["systemctl","is-active",service],capture_output=True,timeout=5,text=True)
        result["services"][service]="active" if r.returncode==0 else "not_active_or_unknown"
    for name,path in (("installed_crontab",Path("/var/spool/cron/crontabs/root")),
                      ("history_service",Path("/etc/systemd/system/tennis-stats-history.service"))):
        result[name]={"present":path.is_file(),"sha256":hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
    # Detect known collectors from process command lines but never emit arguments.
    counts=dict.fromkeys(("detail_job","stats_crosscheck","sweep","health"),0)
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            cmd=path.read_bytes()
            for name in counts:
                if ("tennis_predictor.ingestion.nbbet."+name).encode()in cmd or (name=="health" and b"tennis_predictor.ingestion.health"in cmd):
                    counts[name]+=1
        except (OSError,PermissionError): pass
    result["collector_process_counts"]=counts
    return result,disk_min

def settings(project):
    # Existing production pydantic .env contract; read only allowlisted fields.
    sys.path.insert(0,str(project/"src"))
    from tennis_predictor.config import Settings
    s=Settings(_env_file=str(project/".env"))
    if s.s3_endpoint_url!="https://storage.yandexcloud.net" or s.s3_region!="ru-central1":
        raise Stop("runtime_policy_unverified")
    return s

def snapshot(store,s,guard):
    import psycopg2
    conn=psycopg2.connect(host=s.postgres_host,port=s.postgres_port,user=s.postgres_user,
        password=s.postgres_password,dbname=s.postgres_db,connect_timeout=5,
        application_name="stats-completeness-readonly",
        options="-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=1000 "
                "-c idle_in_transaction_session_timeout=60000 -c timezone=UTC")
    try:
        conn.set_session(readonly=True,isolation_level="REPEATABLE READ",autocommit=False)
        with conn.cursor() as c:
            c.execute("SELECT current_setting('transaction_read_only'),current_setting('transaction_isolation'),CURRENT_TIMESTAMP")
            ro,isolation,now=c.fetchone()
            if (ro,isolation)!=("on","repeatable read"): raise Stop("runtime_policy_unverified")
        with conn.cursor(name="coverage_denominator") as cur:
            cur.itersize=512
            cur.execute("""SELECT match_id::text,nbbet_match_id,EXTRACT(YEAR FROM match_date)::int,
                  gender,level,surface,left(tournament_name,512),status,
                  (duplicate_of IS NULL AND status IN (3,9) AND gender IN ('M','F')
                   AND level IN ('tour','challenger') AND match_date>=DATE '2016-01-01') AS eligible,
                  details_fetched_at,details_version,has_pbp
                FROM public.matches WHERE
                  (duplicate_of IS NULL AND status IN (3,9) AND gender IN ('M','F')
                   AND level IN ('tour','challenger') AND match_date>=DATE '2016-01-01')
                  OR nbbet_match_id=ANY(%s)
                ORDER BY match_id""",(list(core.IDS),))
            while True:
                guard.check()
                rows=cur.fetchmany(512)
                if not rows: break
                store.add_matches([tuple(str(v) if isinstance(v,(date,datetime)) else v for v in row) for row in rows])
        store.set("pg_snapshot",now.isoformat()); store.db.commit()
    except BaseException: store.db.rollback(); raise
    finally: conn.rollback(); conn.close()

def s3client(s,counters,guard):
    import boto3
    from botocore.config import Config
    c=boto3.client("s3",endpoint_url=s.s3_endpoint_url,region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key_id,aws_secret_access_key=s.s3_secret_access_key,
        config=Config(connect_timeout=3,read_timeout=10,retries={"total_max_attempts":1},
                      max_pool_connections=1,s3={"addressing_style":"path"}))
    def allow(model,params,**kw):
        guard.check()
        if model.name not in ("ListObjectsV2","GetObject") or params.get("Bucket")!=s.s3_bucket:
            raise Stop("runtime_policy_unverified")
        if model.name=="GetObject" and (not core.partition(params.get("Key","")) or not params.get("IfMatch")):
            raise Stop("runtime_policy_unverified")
        counters[model.name]=counters.get(model.name,0)+1
    c.meta.events.register("before-parameter-build.s3",allow)
    return c

def inventory(store,c,bucket,kinds,guard):
    store.inventory_start(); bad=0
    for kind in kinds:
        for gender in ("M","F"):
            for level in ("tour","challenger"):
                token=None
                while True:
                    guard.check()
                    kwargs={"Bucket":bucket,"Prefix":f"{kind}/gender={gender}/level={level}/","MaxKeys":1000}
                    if token: kwargs["ContinuationToken"]=token
                    page=c.list_objects_v2(**kwargs)
                    for obj in page.get("Contents",[]):
                        key=obj["Key"]
                        if not key.endswith(".parquet"): continue
                        if not core.partition(key):
                            bad+=1; continue
                        store.inventory_add(key,obj["ETag"],obj["Size"])
                    store.db.commit()
                    if not page.get("IsTruncated"): break
                    next_token=page.get("NextContinuationToken")
                    if not next_token or next_token==token: raise Stop("inventory_unrecognized")
                    token=next_token
    if bad: raise Stop("inventory_unrecognized")
    # Return digest before synchronization, used to check concurrent mutations.
    digest=hashlib.sha256()
    for row in store.db.execute("SELECT * FROM inventory ORDER BY key"): digest.update(core.encoded(row).encode())
    return digest.hexdigest()

def rows(c,bucket,obj,guard,temp,counters):
    import pyarrow.parquet as pq
    key,etag,size,kind,*_=obj
    guard.check(extra_disk=size*2)
    # One bounded disk spool at a time; no in-memory whole object/whole table.
    with tempfile.TemporaryFile(dir=temp) as f:
        response=c.get_object(Bucket=bucket,Key=key,IfMatch=etag)
        body=response["Body"]
        try:
            if response.get("ETag")!=etag or response.get("ContentLength")!=size: raise Stop("object_changed")
            total=0
            while True:
                guard.check()
                block=body.read(1024*1024)
                if not block: break
                total+=len(block); counters["bytes"]=counters.get("bytes",0)+len(block)
                if total>size: raise Stop("object_changed")
                f.write(block)
                # Single worker, <=2 MiB/s nominal download.
                time.sleep(len(block)/(2*1024**2))
            if total!=size: raise Stop("object_changed")
        finally: body.close()
        f.seek(0)
        with pq.ParquetFile(f,thrift_string_size_limit=1024*1024,thrift_container_size_limit=100000) as pf:
            names=pf.schema_arrow.names
            required=["canonical_match_id"]+(["scope"] if kind=="nbbet_stats" else [])
            if not all(k in names for k in required): raise ValueError("missing_join_schema")
            columns=required+([k for k in ("fetched_at",*core.FIELDS) if k in names] if kind=="nbbet_stats" else
                              [k for k in ("fetched_at","sets_json") if k in names] if kind=="nbbet_details" else [])
            for i in range(pf.num_row_groups):
                guard.check()
                rg=pf.metadata.row_group(i)
                projected_bytes=sum(rg.column(j).total_uncompressed_size for j in range(rg.num_columns)
                                    if rg.column(j).path_in_schema in columns)
                if projected_bytes>guard.decode_cap:
                    # No arbitrary sample/truncation; classify entire object unknown.
                    raise Stop("object_decode_guard")
                for batch in pf.iter_batches(batch_size=128,row_groups=[i],columns=columns,use_threads=False):
                    guard.check()
                    if batch.nbytes>guard.decode_cap: raise Stop("object_decode_guard")
                    yield from batch.to_pylist()

def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",default="/root/Tennis_pelmen_model")
    parser.add_argument("--state-dir",default="/var/tmp/tennis-stats-coverage-v1")
    parser.add_argument("--presence",action="store_true",help="Also fully scan raw/PBP ID columns (additional reads)")
    args=parser.parse_args()
    store=None; guard=None; counters={}; code=None; report_complete=False
    try:
        os.umask(0o077)
        os.environ["OPENBLAS_NUM_THREADS"]="1"
        project=Path(args.project).resolve(); work=Path(args.state_dir)
        if work.is_symlink(): raise Stop("runtime_policy_unverified")
        work.mkdir(mode=0o700,parents=True,exist_ok=True)
        if work.stat().st_uid!=os.getuid() or work.stat().st_mode&0o077: raise Stop("runtime_policy_unverified")
        lock=(work/"audit.lock").open("a")
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        os.nice(19)
        pre,disk_min=preflight(project,work)
        guard=Guard(work,disk_min); guard.check()
        # Hard address-space ceiling, in addition to RSS and projected-group guards.
        previous=resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS,(min(2*1024**3,previous[0]) if previous[0]!=resource.RLIM_INFINITY else 2*1024**3,previous[1]))
        for sig in (signal.SIGTERM,signal.SIGINT):
            signal.signal(sig,lambda *_: (_ for _ in ()).throw(Stop("deadline")))
        store=core.Store(work/"coverage.sqlite")
        store.set("preflight",pre); store.set("inventory_stable",False); store.db.commit()
        s=settings(project)
        if not store.get("pg_snapshot"): snapshot(store,s,guard)
        store.set("scan_started",datetime.now(timezone.utc).isoformat())
        c=s3client(s,counters,guard)
        kinds=["nbbet_stats","nbbet_details"]+(["nbbet_raw","pbp"] if args.presence else [])
        first=inventory(store,c,s.s3_bucket,kinds,guard)
        store.inventory_finish(kinds)
        # Do not materialize all rows or keys. Failed objects retry once on next explicit resume.
        cursor=store.db.cursor()
        cursor.execute("SELECT key,etag,size,kind,gender,level,year FROM objects WHERE state!='done' ORDER BY key")
        while True:
            obj=cursor.fetchone()
            if not obj: break
            try: store.ingest(obj[0],rows(c,s.s3_bucket,obj,guard,work,counters))
            except Stop as exc:
                store.fail(obj[0],safe_code(exc))
                if str(exc)in ("deadline","resource_pressure","disk_guard","memory_guard"): raise
            except Exception as exc: store.fail(obj[0],safe_code(exc))
            guard.check()
        last=inventory(store,c,s.s3_bucket,kinds,guard)
        store.set("inventory_stable",first==last); store.db.commit()
    except BaseException as exc:
        code=safe_code(exc)
        # Only fixed codes. Never print exceptions, credentials, raw data or configs.
        print("coverage_audit:"+code,file=sys.stderr)
    finally:
        if store is not None:
            resources=guard.report() if guard else {}
            resources.update(counters)
            store.set("resources",resources)
            store.set("stop_reason",code)
            store.db.commit()
            try:
                report=core.summarize(store,guard)
                output=core.encoded(report)
                # Publication guard: credentials and credential-shaped URLs never leave server.
                if any(v and len(v)>=4 and v in output for v in
                       (s.postgres_password,s.s3_access_key_id,s.s3_secret_access_key)):
                    raise Stop("runtime_policy_unverified")
                if re.search(r"postgres(?:ql)?://|https?://[^/\\s]+:[^@\\s]+@|-----BEGIN .*PRIVATE KEY",output):
                    raise Stop("runtime_policy_unverified")
                if guard: guard.check(extra_disk=len(output.encode())*3)
                core.write_reports(report,Path(args.state_dir)/"report")
                report_complete=report["complete"]
            except Exception: print("coverage_audit:report_failed",file=sys.stderr)
            store.db.close()
    if code: return 1
    # Errors must never produce a successful complete scan exit code.
    return 0 if report_complete else 2

if __name__=="__main__": raise SystemExit(main())
