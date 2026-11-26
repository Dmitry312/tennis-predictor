"""Offline catalog audit. Run: python scripts/audit_ml_catalog.py > report.json
Reads only repository JSON. No application settings, network, ETL or training.
"""
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
SOURCES=("matches","nbbet_details","nbbet_stats","pbp","nbbet_raw")
ADDITIVE=("aces","double_faults","first_serve_points_all","first_serve_points_won",
          "second_serve_points_all","second_serve_points_won","break_points_won",
          "break_points_all","winners","unforced_errors","service_games_won","service_games_all")

def unique(pairs):
    out={}
    for k,v in pairs:
        if k in out: raise ValueError("duplicate_json_key")
        out[k]=v
    return out

def read(path):
    if path.is_symlink() or path.stat().st_size>32*1024*1024:
        raise ValueError("unsafe_or_large_input")
    return json.loads(path.read_bytes(),object_pairs_hook=unique)

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def parse(value):
    if value is None: return None
    return json.loads(value,object_pairs_hook=unique) if isinstance(value,str) else value

def odds(a,b):
    try: return all(math.isfinite(float(v)) and 1<float(v)<=500 for v in (a,b))
    except (ValueError,TypeError,OverflowError): return False

def set_winner(s):
    a,b=s.get("score_team1"),s.get("score_team2")
    if type(a)is not int or type(b)is not int: return None
    hi,lo=max(a,b),min(a,b)
    if not ((hi==6 and 0<=lo<=4) or (hi==7 and lo in (5,6))): return None
    return 1 if a>b else 2

def score_candidate(sets,status):
    # Audit hypothesis BO3 only; never a production target or verified format.
    if status!=3 or not isinstance(sets,list) or len(sets) not in (2,3): return None
    winners=[set_winner(s) for s in sets]
    if None in winners: return None
    counts=Counter()
    for i,w in enumerate(winners):
        if max(counts.values(),default=0)>=2: return None
        counts[w]+=1
    if max(counts.values())!=2: return None
    return counts.most_common(1)[0][0]

def state(v):
    coverage=v.get("coverage")
    if coverage=="object_missing": return "verified_path_object_missing"
    if coverage=="decode_limit": return "decode_limit_skipped"
    if coverage=="partial_row_groups": return "partially_read"
    if coverage=="join_column_missing": return "link_not_established"
    if coverage=="selected_object_only":
        return "selected_rows_present" if v.get("rows") else "verified_object_no_matching_rows"
    return "unavailable_for_verification"

def check_points(points):
    groups=defaultdict(list); findings=[]; seen=set(); point_wins=Counter()
    for p in points:
        key=(p.get("set_num"),p.get("game_num"),p.get("point_num"))
        if key in seen: findings.append({"code":"duplicate_point","key":key,"status":"CONFLICT"})
        seen.add(key); groups[key[:2]].append(p)
        if p.get("point_winner") in (1,2): point_wins[p["point_winner"]]+=1
        if p.get("server") in (1,2) and p.get("point_winner") in (1,2):
            if p.get("server_won_point")!=(p["server"]==p["point_winner"]):
                findings.append({"code":"server_winner_disagreement","key":key,"status":"CONFLICT"})
    sequences={}; last_servers={}
    for key,g in sorted(groups.items()):
        ordered=sorted(g,key=lambda p:p["point_num"])
        if [p["point_num"] for p in ordered]!=list(range(1,len(g)+1)):
            findings.append({"code":"point_sequence_gap","key":key,"status":"CONFLICT"})
        servers={p.get("server") for p in g}
        if not any(p.get("is_tiebreak") for p in g) and len(servers)>1:
            findings.append({"code":"server_changes_inside_game","key":key,"status":"CONFLICT"})
        if not any(p.get("is_tiebreak") for p in g):
            server=ordered[0].get("server")
            previous=last_servers.get(key[0])
            if previous and previous[0]+1==key[1] and previous[1]==server:
                findings.append({"code":"server_did_not_alternate","key":key,"status":"CONFLICT"})
            last_servers[key[0]]=(key[1],server)
        # Stored scores are post-point states. Check only unambiguous one-rank
        # transitions, retaining unknown terminal/deuce/tiebreak states explicitly.
        prev=(0,0)
        ranks={"0":0,"15":1,"30":2,"40":3,"AD":4,"A":4}
        for p in ordered:
            server=p.get("server")
            if server not in (1,2) or p.get("is_tiebreak"):
                continue
            cur=(ranks.get(p.get("score_server")),ranks.get(p.get("score_returner")))
            if None in cur:
                findings.append({"code":"unknown_score_state","key":key,"status":"UNKNOWN"}); continue
            sw=None
            if cur[0]>prev[0] and cur[1]==prev[1]: sw=server
            elif cur[1]>prev[1] and cur[0]==prev[0]: sw=3-server
            elif prev==(4,3) and cur==(3,3): sw=3-server
            elif prev==(3,4) and cur==(3,3): sw=server
            if sw is not None and p.get("point_winner")!=sw:
                findings.append({"code":"score_winner_disagreement","key":key,"status":"CONFLICT"})
            elif sw is None:
                findings.append({"code":"unresolved_score_transition","key":key,"status":"UNKNOWN"})
            prev=cur
        sequences.setdefault(str(key[0]),[]).append(key[1])
    return {"rows":len(points),"games":len(groups),"games_by_set":sequences,
            "point_wins_observed":dict(point_wins),"null_winners":sum(p.get("point_winner")is None for p in points),
            "tiebreak_rows":sum(bool(p.get("is_tiebreak")) for p in points),
            "incomplete_flag_rows":sum(p.get("game_complete")is not True for p in points),
            "point_types":sorted({str(p.get("point_type")) for p in points}),
            "findings":findings,"full_match_verified":False}

def audit(catalog=None):
    catalog=Path(catalog) if catalog else ROOT/"docs/ml/data_catalog"
    index=read(catalog/"index.json"); matches=[]; totals=Counter(); errors=[]
    entries=index.get("real_examples",[])
    ids=[x["match_id"] for x in entries]
    if len(set(ids))!=len(ids): errors.append({"code":"duplicate_index_id","status":"CONFLICT"})
    for entry in sorted(entries,key=lambda e:e["match_id"]):
        mid=entry["match_id"]
        if entry["path"]!="samples/match_"+mid: raise ValueError("unsafe_bundle_path")
        directory=catalog/entry["path"]; manifest=read(directory/"manifest.json")
        findings=[]
        if digest(directory/"manifest.json")!=entry["manifest_sha256"]:
            findings.append({"code":"manifest_hash_mismatch","status":"CONFLICT"})
        envelope=dict(manifest)
        expected_digest=envelope.pop("bundle_digest",None)
        encoded=(json.dumps(envelope,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n").encode()
        if hashlib.sha256(encoded).hexdigest()!=expected_digest:
            findings.append({"code":"bundle_digest_mismatch","status":"CONFLICT"})
        sources={}; rows={}
        for name in SOURCES:
            path=directory/(name+".json"); data=read(path)
            if digest(path)!=manifest.get("files",{}).get(name+".json"):
                findings.append({"code":"file_hash_mismatch","source":name,"status":"CONFLICT"})
            sources[name]=data
            rows[name]=data.get("rows",[]) if name=="matches" else [r for v in data.get("versions",[]) for r in v.get("rows",[])]
            totals[name]+=len(rows[name])
            for i,r in enumerate(rows[name]):
                if r.get("canonical_match_id",r.get("match_id"))!=mid:
                    findings.append({"code":"row_join_mismatch","source":name,"row":i,"status":"CONFLICT"})
        pg=rows["matches"][0] if len(rows["matches"])==1 else {}
        if len(rows["matches"])!=1: findings.append({"code":"pg_row_count","status":"CONFLICT"})
        details=rows["nbbet_details"]; results=[]
        for i,d in enumerate(details):
            sets=parse(d.get("sets_json")); candidate=score_candidate(sets,d.get("status"))
            results.append({"row":i,"sets":sets,"status":d.get("status"),"bo3_score_candidate":candidate,
                            "target_p1_win":None,"format_verified":False})
            if d.get("status")!=pg.get("status"): findings.append({"code":"status_disagreement","status":"CONFLICT"})
            players=parse(d.get("players_json"))
            if players:
                for n in (1,2):
                    if (players.get("p"+str(n)) or {}).get("1")!=pg.get("player"+str(n)):
                        findings.append({"code":"player_orientation_or_alias","player":n,"status":"CONFLICT"})
            for raw in rows["nbbet_raw"]:
                rm=(parse(raw.get("raw_json")) or {}).get("match",{})
                if sets!=rm.get("24"): findings.append({"code":"raw_detail_score_disagreement","status":"CONFLICT"})
                expected=[sum(set_winner(s)==n for s in sets or []) for n in (1,2)]
                if expected!=[rm.get("7"),rm.get("8")]:
                    findings.append({"code":"raw_set_count_disagreement","status":"CONFLICT"})
        stats=rows["nbbet_stats"]; by_scope=defaultdict(list)
        for r in stats: by_scope[r.get("scope")].append(r)
        for scope,values in by_scope.items():
            if len(values)>1: findings.append({"code":"duplicate_stat_scope","scope":scope,"status":"CONFLICT"})
        stat_checks=[]
        if len(by_scope.get(0,[]))==1:
            total=by_scope[0][0]
            for metric in ADDITIVE:
                for n in (1,2):
                    field=metric+"_p"+str(n)
                    parts=[r.get(field) for r in stats if type(r.get("scope"))is int and r["scope"]>0]
                    value=total.get(field)
                    if value is not None and parts and all(v is not None for v in parts):
                        ok=value==sum(parts)
                        stat_checks.append({"field":field,"match":value,"set_sum":sum(parts),"agrees":ok})
                        if not ok: findings.append({"code":"stat_set_sum","field":field,"status":"CONFLICT"})
            for r in stats:
                for n in (1,2):
                    for metric in ("first_serve_points","second_serve_points","break_points","service_games"):
                        won=r.get(metric+"_won_p"+str(n)); all_=r.get(metric+"_all_p"+str(n))
                        if won is not None and all_ is not None and not 0<=won<=all_:
                            findings.append({"code":"invalid_ratio_counts","scope":r["scope"],"metric":metric,"status":"CONFLICT"})
        point=check_points(rows["pbp"])
        for d in details:
            sets=parse(d.get("sets_json")) or []
            for i,s in enumerate(sets,1):
                expected=s.get("score_team1",0)+s.get("score_team2",0)
                observed=len(point["games_by_set"].get(str(i),[]))
                if expected!=observed:
                    findings.append({"code":"pbp_game_count_vs_score","set":i,"expected":expected,"observed":observed,"status":"CONFLICT"})
        serve_total=None
        if len(by_scope.get(0,[]))==1:
            r=by_scope[0][0]; values=[r.get(k+"_p"+str(n)) for n in (1,2) for k in ("first_serve_points_all","second_serve_points_all")]
            if all(v is not None for v in values): serve_total=sum(values)
        if serve_total is not None and serve_total!=len(rows["pbp"]):
            findings.append({"code":"pbp_rows_vs_service_points","observed":len(rows["pbp"]),"stats":serve_total,"status":"CONFLICT"})
        states={}
        for name in SOURCES[1:]:
            versions=sources[name].get("versions",[])
            states[name]=[{"state":state(v),"coverage":v.get("coverage"),"rows":len(v.get("rows",[])),
                           "reasons":v.get("decode_reasons",[]),"object_key":v.get("object_key"),
                           "rows_truncated":v.get("rows_truncated"),"scan_complete":v.get("object_scan_complete")} for v in versions]
            if not versions: states[name]=[{"state":"unavailable_for_verification","reason":"no_checked_object_in_catalog"}]
        matches.append({"match_id":mid,"evidence":str(entry["path"]),"metadata":pg,
                        "counts":{k:len(v) for k,v in rows.items()},"source_states":states,
                        "odds_pair_valid":odds(pg.get("odds_open_p1"),pg.get("odds_open_p2")),
                        "results":results,"stat_additivity":stat_checks,"pbp":point,
                        "findings":findings,"coverage_complete":False})
    plans=[]
    for path in sorted((catalog/"acquisition-plans").glob("*.json")):
        p=read(path); selected=p.get("selected_ids",[])
        plans.append({"file":path.name,"selected":len(selected),"catalogued":sum(i in ids for i in selected),
                      "uncatalogued":[i for i in selected if i not in ids]})
    return {"audit_version":"catalog-semantic-audit-v1","scope":"repository_snapshot_only",
            "match_count":len(matches),"records_by_source":dict(totals),"plans":plans,
            "matches":matches,"findings":errors,"production_access":False}

def main():
    try:
        result=audit()
        print(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False))
        return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(json.dumps({"audit_failed":True,"error_type":type(exc).__name__}),file=sys.stderr)
        return 1
if __name__=="__main__": raise SystemExit(main())
