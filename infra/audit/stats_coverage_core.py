"""Resumable statistics coverage, not statistics correctness/crosscheck.
Only local SQLite writes. Source clients restricted to SELECT/LIST/GET.
"""
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

VERSION="stats-coverage-v1"
IDS=(249197,26041,577961,81415,335934,725766,94721,453599,135037,691300,605600,571806)
METRICS=("aces","double_faults","first_serve_points_all","first_serve_points_won",
 "second_serve_points_all","second_serve_points_won","break_points_won","break_points_all",
 "winners","unforced_errors","max_points_in_row","max_games_in_row",
 "service_games_won","service_games_all")
FIELDS=tuple(f"{m}_p{p}" for m in METRICS for p in (1,2))
# Source-optional metrics remain in the 28-field strict view.
CORE=tuple(f"{m}_p{p}" for m in METRICS if m not in
 ("break_points_won","break_points_all","winners","unforced_errors","max_points_in_row","max_games_in_row")
 for p in (1,2))
KEY=re.compile(r"^(nbbet_stats|nbbet_details|nbbet_raw|pbp)/gender=([MF])/level=(tour|challenger)/year=(\d{4})/[^/]+\.parquet$")

def encoded(v): return json.dumps(v,sort_keys=True,separators=(",",":"),allow_nan=False)
def stamp(v):
    if v is None: return ""
    if isinstance(v,datetime): d=v
    elif isinstance(v,str): d=datetime.fromisoformat(v.replace("Z","+00:00"))
    else: raise ValueError("invalid_fetched_at")
    # Existing source writes naive UTC datetime; normalize for ordering.
    if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()

def partition(key):
    m=KEY.fullmatch(key)
    return m.groups() if m and int(m[4])>=2016 else None

def metric(v):
    if v is None: return None
    if isinstance(v,float) and math.isnan(v): return None
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):
        raise ValueError("metric_schema_unusable")
    return float(v)

class Store:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA cache_size=-8192;
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT);
        CREATE TABLE IF NOT EXISTS matches(mid TEXT PRIMARY KEY,nbid INTEGER,year INTEGER,
          gender TEXT,level TEXT,surface TEXT,tournament TEXT,status INTEGER,eligible INTEGER,
          details_at TEXT,details_version INTEGER,pbp_flag INTEGER);
        CREATE TABLE IF NOT EXISTS objects(key TEXT PRIMARY KEY,etag TEXT,size INTEGER,
          kind TEXT,gender TEXT,level TEXT,year INTEGER,state TEXT,error TEXT,rows INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS inventory(key TEXT PRIMARY KEY,etag TEXT,size INTEGER,
          kind TEXT,gender TEXT,level TEXT,year INTEGER);
        CREATE TABLE IF NOT EXISTS records(object TEXT,ordinal INTEGER,mid TEXT,scope INTEGER,
          ts TEXT,payload TEXT,PRIMARY KEY(object,ordinal));
        CREATE INDEX IF NOT EXISTS record_key ON records(mid,scope,ts);
        """)
        old=self.get("version")
        if old and old!=VERSION: raise ValueError("checkpoint_version")
        self.set("version",VERSION); self.db.commit()
    def set(self,k,v): self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",(k,encoded(v)))
    def get(self,k,default=None):
        r=self.db.execute("SELECT v FROM meta WHERE k=?",(k,)).fetchone()
        return json.loads(r[0]) if r else default
    def add_matches(self,rows):
        self.db.executemany("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",rows)
    def inventory_start(self):
        self.db.execute("DELETE FROM inventory"); self.db.commit()
    def inventory_add(self,key,etag,size):
        p=partition(key)
        if p: self.db.execute("INSERT INTO inventory VALUES (?,?,?,?,?,?,?)",
                            (key,etag,size,p[0],p[1],p[2],int(p[3])))
    def inventory_finish(self,kinds):
        # Same frozen PostgreSQL denominator, new complete S3 inventory on resume.
        removed=self.db.execute("SELECT key FROM objects WHERE kind IN ("+
            ",".join("?" for _ in kinds)+") AND key NOT IN (SELECT key FROM inventory)",kinds).fetchall()
        for (key,) in removed:
            self.db.execute("DELETE FROM records WHERE object=?",(key,))
            self.db.execute("DELETE FROM objects WHERE key=?",(key,))
        for key,etag,size,kind,gender,level,year in self.db.execute("SELECT * FROM inventory").fetchall():
            old=self.db.execute("SELECT etag,size FROM objects WHERE key=?",(key,)).fetchone()
            if old!=(etag,size):
                self.db.execute("DELETE FROM records WHERE object=?",(key,))
                self.db.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?,?,?,'pending',NULL,0)",
                                (key,etag,size,kind,gender,level,year))
        self.set("inventory_complete",True); self.set("scanned_kinds",list(kinds)); self.db.commit()
    def ingest(self,key,rows):
        obj=self.db.execute("SELECT kind FROM objects WHERE key=?",(key,)).fetchone()
        kind=obj[0]; count=0
        self.db.execute("DELETE FROM records WHERE object=?",(key,))
        try:
            for i,r in enumerate(rows):
                mid=str(r.get("canonical_match_id") or "")
                if not mid: raise ValueError("join_column_unusable")
                scope=r.get("scope") if kind=="nbbet_stats" else 0
                if kind=="nbbet_stats" and (isinstance(scope,bool) or not isinstance(scope,int) or scope<0):
                    raise ValueError("scope_unusable")
                ts=stamp(r.get("fetched_at"))
                if kind=="nbbet_stats": payload={k:metric(r.get(k)) for k in FIELDS}
                elif kind=="nbbet_details":
                    raw=r.get("sets_json")
                    sets=json.loads(raw) if isinstance(raw,str) else raw
                    payload={"expected_sets":len(sets) if isinstance(sets,list) and sets else None}
                else: payload={"physical_rows":True}
                self.db.execute("INSERT INTO records VALUES (?,?,?,?,?,?)",
                                (key,i,mid,int(scope),ts,encoded(payload)))
                count+=1
            self.db.execute("UPDATE objects SET state='done',error=NULL,rows=? WHERE key=?",(count,key))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
    def fail(self,key,code):
        self.db.execute("DELETE FROM records WHERE object=?",(key,))
        self.db.execute("UPDATE objects SET state='error',error=? WHERE key=?",(code,key))
        self.db.commit()
    def latest(self,mid,kind):
        out={}
        query="""SELECT r.scope,r.ts,r.payload FROM records r
          JOIN objects o ON o.key=r.object WHERE r.mid=? AND o.kind=? AND o.state='done'
          ORDER BY r.scope,r.ts DESC,r.payload"""
        for scope,ts,payload in self.db.execute(query,(mid,kind)):
            if scope not in out: out[scope]={"ts":ts,"payload":json.loads(payload),"conflict":False,"versions":1}
            else:
                out[scope]["versions"]+=1
                if ts==out[scope]["ts"] and json.loads(payload)!=out[scope]["payload"]:
                    out[scope]["conflict"]=True
        return out
    def complete(self,year,gender,level,kind):
        if kind not in self.get("scanned_kinds",[]) or not self.get("inventory_complete") or not self.get("inventory_stable",False): return False
        # A malformed/mispartitioned object may contain any match ID.
        # Conservatively taint all groups of that source when any object is unread.
        return not self.db.execute("SELECT 1 FROM objects WHERE kind=? AND state!='done' LIMIT 1",(kind,)).fetchone()

    def coverage(self,m):
        mid,nbid,year,gender,level,surface,tournament,status,eligible,details_at,details_version,pbp_flag=m
        versions=self.latest(mid,"nbbet_stats")
        done=self.complete(year,gender,level,"nbbet_stats")
        conflict=any(v["conflict"] for v in versions.values())
        known=done and not conflict
        scope0=versions.get(0)
        present=bool(versions)
        core=bool(scope0 and not scope0["conflict"] and all(scope0["payload"][f]is not None for f in CORE))
        strict=bool(scope0 and not scope0["conflict"] and all(scope0["payload"][f]is not None for f in FIELDS))
        useful=bool(scope0 and not scope0["conflict"] and any(v is not None for v in scope0["payload"].values()))
        detail=self.latest(mid,"nbbet_details").get(0)
        detail_complete=self.complete(year,gender,level,"nbbet_details")
        expected=detail["payload"].get("expected_sets") if detail and not detail["conflict"] and detail_complete else None
        scopes=sorted(s for s in versions if s>0)
        exact=expected is not None and scopes==list(range(1,expected+1))
        set_core=exact and all(not versions[s]["conflict"] and all(versions[s]["payload"][f]is not None for f in CORE) for s in scopes)
        category="unknown" if not known else "absent" if not present else "full" if core and set_core else "partial"
        return {"mid":mid,"nbbet_match_id":nbid,"year":year,"gender":gender,"level":level,
                "surface":surface,"tournament":tournament,"status":status,"eligible":bool(eligible),
                "any_stats_observed":present,"scope0_observed":scope0 is not None,
                "usable_scope0_observed":useful,"core_scope0":core,"strict28_scope0":strict,
                "set_scopes":scopes,"expected_sets":expected,"sets_complete":exact if expected is not None else None,
                "set_core_complete":set_core if expected is not None else None,
                "latest_proven":known,"version_conflict":conflict,"category":category,
                "versions":sum(v["versions"] for v in versions.values()),
                "details_pg_fetched":details_at is not None,"details_pg_version":details_version,
                "pbp_pg_flag":bool(pbp_flag),
                "physical_presence":{k:("present_observed" if self.latest(mid,k) else
                    "not_found_complete_scan" if self.complete(year,gender,level,k) else "unknown_not_fully_scanned")
                    for k in ("nbbet_details","nbbet_raw","pbp")}}

def pct(n,d): return round(100*n/d,4) if d else None

def summarize(store,guard=None):
    groups={}; controls=[]; allcounts={}
    for i,m in enumerate(store.db.execute("SELECT * FROM matches ORDER BY mid")):
        if guard and i%128==0: guard.check()
        c=store.coverage(m)
        if c["nbbet_match_id"] in IDS: controls.append(c)
        if not c["eligible"]: continue
        keys=[("overall",c["status"]),("year_gender_level",c["year"],c["gender"],c["level"],c["status"]),
              ("year_gender_level_surface",c["year"],c["gender"],c["level"],c["surface"],c["status"]),
              ("tournament",c["year"],c["gender"],c["level"],c["tournament"],c["status"])]
        latest=store.latest(c["mid"],"nbbet_stats")
        for key in keys:
            k=encoded(key)
            g=groups.setdefault(k,{"group":key,"denominator":0,
                **{n:0 for n in ("full","partial","absent","unknown","any_stats_observed","scope0_observed",
                "usable_scope0_observed","core_scope0","strict28_scope0","set_rows","set_matches",
                "expected_sets_known","expected_set_count","set_scopes_complete","set_core_complete",
                "version_conflicts","versions","confirmed_stats_present")},
                "fields_match":dict.fromkeys(FIELDS,0),"fields_set":dict.fromkeys(FIELDS,0)})
            g["denominator"]+=1
            g[c["category"]]+=1
            for field in ("any_stats_observed","scope0_observed","usable_scope0_observed","core_scope0","strict28_scope0"):
                g[field]+=int(c[field])
            g["confirmed_stats_present"]+=int(c["latest_proven"] and c["usable_scope0_observed"])
            g["set_matches"]+=bool(c["set_scopes"])
            g["expected_sets_known"]+=c["expected_sets"]is not None
            g["expected_set_count"]+=c["expected_sets"] or 0
            g["set_scopes_complete"]+=c["sets_complete"] is True
            g["set_core_complete"]+=c["set_core_complete"] is True
            g["version_conflicts"]+=c["version_conflict"]
            g["versions"]+=c["versions"]
            for scope,v in latest.items():
                if scope>0: g["set_rows"]+=1
                if v["conflict"]: continue
                bucket=g["fields_set"] if scope else g["fields_match"]
                for f in FIELDS: bucket[f]+=v["payload"][f]is not None
    for g in groups.values():
        g["percent"]={k:pct(g[k],g["denominator"]) for k in
             ("full","partial","absent","unknown","any_stats_observed","scope0_observed",
              "usable_scope0_observed","core_scope0","strict28_scope0","confirmed_stats_present")}
        g["fields_match_percent_all_matches"]={f:pct(n,g["denominator"]) for f,n in g["fields_match"].items()}
        g["fields_set_percent_observed_rows"]={f:pct(n,g["set_rows"]) for f,n in g["fields_set"].items()}
        g["fields_set_percent_expected_scopes"]={f:pct(n,g["expected_set_count"]) for f,n in g["fields_set"].items()} if g["unknown"]==0 and g["expected_sets_known"]==g["denominator"] and g["set_rows"]<=g["expected_set_count"] else None
        g["coverage_proven"]=g["unknown"]==0
    errors=[dict(zip(("key","state","error"),r)) for r in store.db.execute(
        "SELECT key,state,error FROM objects WHERE state!='done' ORDER BY key")]
    unmatched=store.db.execute("""SELECT COUNT(DISTINCT r.mid) FROM records r
         LEFT JOIN matches m ON m.mid=r.mid WHERE m.mid IS NULL""").fetchone()[0]
    return {"version":VERSION,"pg_snapshot":store.get("pg_snapshot"),"scan_started":store.get("scan_started"),
        "inventory_stable":store.get("inventory_stable",False),"inventory_complete":store.get("inventory_complete",False),
        "complete":store.get("inventory_complete",False) and store.get("inventory_stable",False) and not errors,
        "object_counts":[dict(zip(("kind","state","objects","rows"),r)) for r in store.db.execute(
            "SELECT kind,state,COUNT(*),SUM(rows) FROM objects GROUP BY kind,state ORDER BY kind,state")],
        "groups":[groups[k] for k in sorted(groups)],"controls":controls,
        "control_ids_not_in_pg_snapshot":sorted(set(IDS)-{c["nbbet_match_id"] for c in controls}),
        "unmatched_distinct_ids":unmatched,"incomplete_objects":errors,
        "full_definition":{"core_fields":CORE,"strict_fields":FIELDS,
          "full":"latest proven, core scope0 and exact expected set scopes with core fields",
          "optional":"BP, winners/errors, maxima excluded only from core; still audited individually"},
        "resources":store.get("resources",{}),"preflight":store.get("preflight",{}),"stop_reason":store.get("stop_reason"),
        "limitations":["S3 inventory is not an atomic snapshot with PG",
          "presence observations are lower bounds until all matching objects are read",
          "absence refers to scanned inventory, never live-source absence",
          "stats values not crosschecked; no live NBBet requests"]}

def write_reports(report,out):
    out=Path(out); out.mkdir(parents=True,exist_ok=True)
    (out/"coverage.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    with (out/"coverage.csv").open("w",encoding="utf-8",newline="") as f:
        writer=csv.writer(f); columns=("denominator","confirmed_stats_present","any_stats_observed","scope0_observed",
              "core_scope0","strict28_scope0","set_matches","set_scopes_complete","full","partial","absent","unknown")
        writer.writerow(("group",*columns,"confirmed_stats_percent","coverage_proven"))
        for g in report["groups"]: writer.writerow((encoded(g["group"]),*(g[c] for c in columns),
            g["percent"]["confirmed_stats_present"],g["coverage_proven"]))
    with (out/"fields.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.writer(f); w.writerow(("group","scope","field","non_null","denominator","percent","denominator_basis","latest_proven"))
        for g in report["groups"]:
            for scope in ("match","set"):
                den=g["denominator"] if scope=="match" else g["set_rows"]
                for field,n in g["fields_"+scope].items():
                    w.writerow((encoded(g["group"]),scope,field,n,den,pct(n,den),
                                "all_eligible_matches" if scope=="match" else "observed_latest_set_scopes",
                                g["coverage_proven"]))
    lines=["# Statistics coverage audit","","Complete: "+str(report["complete"]),
           "PG snapshot: "+str(report["pg_snapshot"]),"",
           "Observed counts are NOT full coverage when latest_proven/coverage_proven is false.","",
           "| Group | Matches | Confirmed usable scope0 | % | Full core | Partial | Absent | Unknown |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for g in report["groups"]:
        if g["group"][0]in ("overall","year_gender_level"):
            lines.append("| "+str(g["group"])+" | "+" | ".join(str(g[k]) for k in
                  ("denominator","confirmed_stats_present"))+" | "+str(g["percent"]["confirmed_stats_present"])+" | "+
                  " | ".join(str(g[k]) for k in ("full","partial","absent","unknown"))+" |")
    lines+=["","Tables by surface/tournament and field denominators: coverage.csv, fields.csv and coverage.json.",
            "No production-data writes or source-value validation performed."]
    (out/"README.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
