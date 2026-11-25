"""Bounded real sports-record export, local publication and offline reuse.

No automatic git operations, production workflow or connection defaults.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, date
from decimal import Decimal
import hashlib
import io
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Reuse only representation/part-layout primitives, never the raw production driver.
from infra.audit.full_match_export import json_value, related_partition_keys

SOURCES = ("nbbet_details", "nbbet_stats", "pbp", "nbbet_raw")
FORMAT = "real-match-samples-v1"
FILES = ("matches.json",) + tuple(s+".json" for s in SOURCES)
SECRET_KEYS = re.compile(r"(?:password|passwd|secret|token|authorization|cookie|credential|privatekey|apikey|accesskey|email|phone|address|sessionid)", re.I)
KEYS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
        "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")


class ExportError(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise ExportError(code)


def mid(value):
    try:
        need(isinstance(value, (str, uuid.UUID)), "invalid_match_id")
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ExportError("invalid_match_id") from None


def unique(pairs):
    result = {}
    for k, v in pairs:
        need(k not in result, "duplicate_json_key")
        result[k] = v
    return result


def dumps(value):
    try:
        return (json.dumps(value, default=json_value, ensure_ascii=False,
                           sort_keys=True, indent=2, allow_nan=False)+"\n").encode("utf-8")
    except (ValueError, TypeError, OverflowError):
        raise ExportError("serialization_failed") from None


def read_json(path, cap):
    with Path(path).open("rb") as stream:
        raw = stream.read(cap+1)
    need(len(raw) <= cap, "input_limit")
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_float=Decimal)
    except (ValueError, UnicodeError, RecursionError):
        raise ExportError("invalid_json") from None


def inspect_content(value, secrets=(), depth=0, nodes=None):
    """Reject entire staging operation; do not rewrite sports fields/JSON strings."""
    nodes = nodes if nodes is not None else [0]
    nodes[0] += 1
    need(nodes[0] <= 2000000 and depth <= 32, "content_limit")
    if isinstance(value, dict):
        for k, v in value.items():
            need(type(k) is str, "invalid_key")
            normalized = re.sub("[^a-z0-9]", "", k.lower())
            need(not SECRET_KEYS.search(normalized), "sensitive_content")
            inspect_content(k, secrets, depth+1, nodes)
            inspect_content(v, secrets, depth+1, nodes)
    elif isinstance(value, (list, tuple)):
        for v in value:
            inspect_content(v, secrets, depth+1, nodes)
    elif isinstance(value, bytes):
        inspect_content(value.decode("utf-8", errors="replace"), secrets, depth+1, nodes)
    elif isinstance(value, str):
        need(not any(secret and (secret in value if len(secret) >= 8 else secret == value)
                     for secret in secrets), "sensitive_content")
        need(not re.search(r"-----BEGIN .*PRIVATE KEY|(?:postgres(?:ql)?|mysql)://|"
                           r"https?://[^/\s]+:[^@\s]+@|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
                           r"(?:[?&](?:token|key|signature|credential|password)=)", value, re.I), "sensitive_content")
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                nested = json.loads(value, object_pairs_hook=unique)
            except (ValueError, RecursionError):
                # Malformed sports JSON is preserved, but obvious forbidden key labels still block.
                need(not re.search(r'"(?:password|token|secret|email|phone|authorization)"\s*:', value, re.I),
                     "sensitive_content")
            else:
                inspect_content(nested, secrets, depth+1, nodes)


@dataclass(frozen=True)
class Limits:
    matches: int = 12
    objects: int = 60
    object_bytes: int = 8*1024*1024
    total_bytes: int = 64*1024*1024
    file_rows: int = 100000
    total_rows: int = 300000
    source_rows: int = 5000
    output_bytes: int = 32*1024*1024
    uncompressed_bytes: int = 128*1024*1024
    seconds: int = 120

    def validate(self):
        caps = {"matches":24, "objects":100, "object_bytes":32*1024*1024,
                "total_bytes":256*1024*1024, "file_rows":500000, "total_rows":1000000,
                "source_rows":20000, "output_bytes":128*1024*1024,
                "uncompressed_bytes":256*1024*1024, "seconds":300}
        need(all(type(v) is int and 0 < v <= caps[k] for k,v in asdict(self).items()), "invalid_limits")


class Budget:
    def __init__(self, limits):
        limits.validate()
        self.limits = limits
        self.end = time.monotonic()+limits.seconds
        self.calls = self.bytes = self.rows = self.sent = self.http = 0
    def check(self):
        need(time.monotonic() < self.end, "deadline")
    def charge(self, size=0):
        self.check()
        need(self.calls < self.limits.objects*2 and self.bytes+size <= self.limits.total_bytes, "read_budget")
        self.calls += 1
        self.bytes += size
        self.sent = 0


def config(path=None, environ=None):
    values = {}
    if path is not None:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        need(len(raw) <= 65536, "config_limit")
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            m = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
            if not m:
                need(not any(re.match(r"(?:export\s+)?"+k+r"\b", line) for k in KEYS), "config_invalid")
                continue
            k,v = m.groups()
            if k not in KEYS: continue
            need(k not in values, "config_duplicate")
            v = v.strip()
            if v.startswith(("'", '"')):
                end = v.find(v[0],1)
                need(end > 0 and (not v[end+1:].strip() or v[end+1:].lstrip().startswith("#")), "config_invalid")
                v = v[1:end]
            else:
                need(re.fullmatch(r"""[^\s#"'\\]+""",v) is not None, "config_invalid")
            values[k] = v
    env = os.environ if environ is None else environ
    for k in KEYS:
        if k in env:
            need(k not in values or values[k] == env[k], "config_ambiguous")
            values[k] = env[k]
    need(all(type(values.get(k)) is str and 0 < len(values[k]) <= 4096 and
             not any(c in values[k] for c in ("\x00","\r","\n","$","\\",chr(96))) for k in KEYS), "config_missing")
    need(re.fullmatch(r"[0-9]{1,5}",values["POSTGRES_PORT"]) is not None and
         1 <= int(values["POSTGRES_PORT"]) <= 65535, "config_invalid")
    need(values["S3_ENDPOINT_URL"] == "https://storage.yandexcloud.net" and
         values["S3_REGION"] == "ru-central1", "config_endpoint")
    bucket = values["S3_BUCKET"]
    need(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",bucket) is not None and ".." not in bucket, "config_bucket")
    try: ipaddress.ip_address(bucket)
    except ValueError: return values
    raise ExportError("config_bucket")


def check_session(connection):
    with connection.cursor() as c:
        c.execute("SELECT current_setting('transaction_read_only'), current_setting('statement_timeout') LIMIT 1")
        rows = c.fetchmany(2)
    need(len(rows) == 1 and rows[0][0] == "on", "readonly_required")
    m = re.fullmatch(r"([0-9]+)(ms|s)?", rows[0][1])
    need(m is not None and 0 < int(m[1])*(1000 if m[2]=="s" else 1) <= 5000, "sql_timeout_required")


def select_ids(connection, ids, limits, *, date_from=None, date_to=None, player=None, tournament=None):
    check_session(connection)
    need(not (ids and any(x is not None for x in (date_from,date_to,player,tournament))), "selection_ambiguous")
    if ids:
        result = sorted(set(mid(x) for x in ids))
        need(0 < len(result) <= limits.matches, "match_limit")
        return result, {"mode":"exact_uuid","truncated":False}
    need(any(x is not None for x in (date_from,date_to,player,tournament)), "selection_required")
    clauses, params = ["duplicate_of IS NULL"], []
    if date_from is not None:
        start = date.fromisoformat(date_from)
        need(date_to is not None, "date_range_required")
        end = date.fromisoformat(date_to)
        need(0 <= (end-start).days <= 31, "date_range_limit")
        clauses.extend(("match_date >= %s", "match_date <= %s"))
        params.extend((start,end))
    else:
        need(date_to is None, "date_range_required")
    if player is not None:
        need(type(player) is str and 0 < len(player) <= 200, "selection_invalid")
        clauses.append("(player1 = %s OR player2 = %s)")
        params.extend((player,player))
    if tournament is not None:
        need(type(tournament) is str and 0 < len(tournament) <= 512, "selection_invalid")
        clauses.append("tournament_name = %s")
        params.append(tournament)
    query = "SELECT match_id::text FROM public.matches WHERE "+" AND ".join(clauses)+" ORDER BY match_id LIMIT %s"
    params.append(limits.matches+1)
    with connection.cursor() as c:
        c.execute("EXPLAIN (FORMAT JSON) "+query,tuple(params))
        plans = c.fetchmany(2)
        need(len(plans)==1, "selection_plan")
        root = plans[0][0][0]["Plan"]
        need(root.get("Total Cost",float("inf")) <= 10000, "selection_unindexed")
        nodes, indexed, conditions = [root], False, []
        while nodes:
            node = nodes.pop()
            conditions.append(str(node.get("Index Cond", "")))
            if node.get("Relation Name") == "matches":
                need(node.get("Node Type") not in ("Seq Scan","Parallel Seq Scan"), "selection_unindexed")
                indexed = indexed or "Index" in node.get("Node Type","") or node.get("Node Type") == "Bitmap Heap Scan"
            nodes.extend(node.get("Plans",[]))
        need(indexed and any(any(field in cond for field in ("match_date", "player1", "player2", "tournament_name"))
                             for cond in conditions), "selection_unindexed")
        c.execute(query,tuple(params))
        found = c.fetchmany(limits.matches+2)
    need(len(found) <= limits.matches+1, "selection_limit")
    return sorted(mid(r[0]) for r in found[:limits.matches]), {"mode":"bounded_indexed_filter",
        "truncated":len(found)>limits.matches,"date_from":date_from,"date_to":date_to,
        "player":player,"tournament":tournament}


def read_pg(connection, ids, limits):
    check_session(connection)
    if not ids: return {}, [], {"candidates":[], "truncated":False}
    pg_bytes=min(limits.output_bytes,1024*1024)
    with connection.cursor() as c:
        c.execute("SELECT match_id::text, left(row_to_json(m)::text, %s) FROM public.matches m "
                  "WHERE match_id = ANY(%s::uuid[]) ORDER BY match_id LIMIT %s",(pg_bytes+1,ids,limits.matches+1))
        rows = c.fetchmany(limits.matches+2)
        need(len(rows)<=limits.matches, "sql_row_limit")
        result = {}
        for key, raw in rows:
            need(type(raw)is str and len(raw.encode("utf-8"))<=pg_bytes,"sql_byte_limit")
            key = mid(key)
            need(key in ids and key not in result, "join_conflict")
            result[key] = json.loads(raw,object_pairs_hook=unique,parse_float=Decimal)
        c.execute("SELECT column_name, data_type, udt_name, numeric_precision, numeric_scale "
                  "FROM information_schema.columns WHERE table_schema='public' AND table_name='matches' "
                  "ORDER BY ordinal_position LIMIT 129")
        schema = c.fetchmany(130)
        need(len(schema)<=128, "schema_limit")
        c.execute("SELECT table_name, column_name FROM information_schema.columns "
                  "WHERE table_schema='public' AND table_name <> 'matches' "
                  "AND column_name IN ('match_id','canonical_match_id') ORDER BY 1,2 LIMIT 129")
        inventory = c.fetchmany(130)
        need(len(inventory)<=129, "inventory_limit")
    return result, [dict(zip(("name","data_type","udt_name","numeric_precision","numeric_scale"),row)) for row in schema], {
        "candidates":[{"table":r[0],"column":r[1],"basis":"column_name_candidate_not_verified_fk",
                       "rows_exported":False} for r in inventory[:128]], "truncated":len(inventory)>128}


def object_plan(raw, ids, limits):
    need(type(raw) is dict and raw.get("manifest_version") == 1 and type(raw.get("matches")) is dict, "manifest_invalid")
    need(set(raw["matches"]) <= set(ids), "manifest_selection_mismatch")
    objects = {}
    for key, sources in raw["matches"].items():
        need(mid(key)==key and type(sources) is dict and set(sources)<=set(SOURCES), "manifest_invalid")
        for source, versions in sources.items():
            need(type(versions) is list and len(versions)<=limits.objects, "manifest_limit")
            for v in versions:
                need(type(v) is dict and set(v)<= {"key","version_id","etag","current"} and
                     type(v.get("key")) is str and len(v["key"].encode()) <= 1024 and
                     v["key"].startswith(source+"/") and v["key"].endswith(".parquet") and
                     ".." not in v["key"].split("/"), "object_key_invalid")
                need((type(v.get("version_id")) is str and 0<len(v["version_id"])<=1024) or
                     (type(v.get("etag")) is str and 0<len(v["etag"])<=256) or v.get("current") is True,
                     "version_selection_required")
                ident = (source,v["key"],v.get("version_id"))
                if ident in objects: need(objects[ident]["spec"]==v,"version_selection_conflict")
                objects.setdefault(ident,{"spec":dict(v),"match_ids":set()})["match_ids"].add(key)
    need(len(objects)<=limits.objects,"object_limit")
    return objects


class S3Reader:
    def __init__(self, client, bucket, planned, budget):
        self.client,self.bucket,self.planned,self.budget=client,bucket,planned,budget
        client.meta.events.register("before-parameter-build.s3",self.guard)
        client.meta.events.register("before-send.s3",self.before_send)
    def guard(self,model,params,**kwargs):
        need(model.name in ("HeadObject","GetObject") and params.get("Bucket")==self.bucket,"operation_forbidden")
        need(any(params.get("Key")==ident[1] and params.get("VersionId")==ident[2] for ident in self.planned),"operation_forbidden")
        if model.name=="GetObject": need(bool(params.get("IfMatch")),"conditional_read_required")
    def before_send(self,request,**kwargs):
        from urllib.parse import urlsplit
        self.budget.check()
        u=urlsplit(request.url)
        need(u.scheme=="https" and u.hostname=="storage.yandexcloud.net" and
             u.port in (None,443) and not u.username and not u.password,"destination_forbidden")
        need(self.budget.sent==0 and self.budget.http<self.budget.limits.objects*2,"retry_forbidden")
        self.budget.sent+=1
        self.budget.http+=1
    def call(self,op,**params):
        class Model:
            name={"head_object":"HeadObject","get_object":"GetObject"}.get(op)
        self.guard(Model,{"Bucket":self.bucket,**params})
        return getattr(self.client,op)(Bucket=self.bucket,**params)
    def read(self,ident,entry):
        import pyarrow as pa
        import pyarrow.parquet as pq
        source,key,version=ident
        spec=entry["spec"]
        params={"Key":key}
        if version is not None: params["VersionId"]=version
        if spec.get("etag"): params["IfMatch"]=spec["etag"]
        self.budget.charge()
        try: head=self.call("head_object",**params)
        except Exception as exc:
            if getattr(exc,"response",{}).get("ResponseMetadata",{}).get("HTTPStatusCode")==404:
                return {"coverage":"object_missing","object_key":key,"requested_version":spec,"rows":[]}
            raise ExportError("s3_head_failed") from None
        size,tag=head.get("ContentLength"),head.get("ETag")
        need(type(size)is int and 12<=size<=self.budget.limits.object_bytes and type(tag)is str and bool(tag),"object_limit")
        if spec.get("etag"): need(tag==spec["etag"],"object_changed")
        self.budget.charge(size)
        params["IfMatch"]=tag
        response=self.call("get_object",**params)
        body=response.get("Body")
        try:
            need(response.get("ResponseMetadata",{}).get("HTTPStatusCode")==200 and
                 response.get("ContentLength")==size and response.get("ETag")==tag,"object_changed")
            if version is not None: need(response.get("VersionId")==version,"object_changed")
            data=body.read(size)
            self.budget.check()
            need(len(data)==size and data[:4]==b"PAR1" and data[-4:]==b"PAR1","invalid_parquet")
        finally:
            if body is not None: body.close()
        rows={key:[] for key in entry["match_ids"]}
        totals={key:0 for key in entry["match_ids"]}
        with pq.ParquetFile(pa.BufferReader(data),thrift_string_size_limit=1024*1024,
                            thrift_container_size_limit=100000) as f:
            m=f.metadata
            need(m.num_rows<=self.budget.limits.file_rows and m.num_columns<=128 and m.num_row_groups<=128,"decode_limit")
            need(self.budget.rows+m.num_rows<=self.budget.limits.total_rows and
                 sum(m.row_group(i).total_byte_size for i in range(m.num_row_groups))<=self.budget.limits.uncompressed_bytes,"decode_limit")
            self.budget.rows+=m.num_rows
            schema=[{"name":field.name,"type":str(field.type),"nullable":field.nullable} for field in f.schema_arrow]
            need(len(f.schema_arrow.names)==len(set(f.schema_arrow.names)),"duplicate_column")
            coverage="selected_object_only"
            if "canonical_match_id" not in f.schema_arrow.names:
                coverage="join_column_missing"
            else:
                for batch in f.iter_batches(batch_size=256,use_threads=False):
                    need(batch.nbytes<=self.budget.limits.uncompressed_bytes,"decode_limit")
                    for row in batch.to_pylist():
                        try: row_id=mid(row.get("canonical_match_id"))
                        except ExportError: continue
                        if row_id in rows:
                            totals[row_id]+=1
                            if len(rows[row_id])<self.budget.limits.source_rows: rows[row_id].append(row)
                    self.budget.check()
        return {"coverage":coverage,"object_key":key,"requested_version":spec,
                "etag":tag,"version_id":response.get("VersionId"),"content_sha256":hashlib.sha256(data).hexdigest(),
                "last_modified":head.get("LastModified"),"schema":schema,"rows_by_match":rows,
                "matched_rows":totals}


def bundles(ids, pg, pg_schema, inventory, results, selection, limits):
    output={}
    for key in sorted(ids):
        data={"matches.json":{"match_id":key,"rows":[pg[key]] if key in pg else [],
                              "coverage":"exact_primary_key" if key in pg else "row_missing","schema":pg_schema}}
        for source in SOURCES:
            versions=[]
            for ident,result in sorted(results.items(),key=lambda kv:(kv[0][0],kv[0][1],kv[0][2] or "")):
                if ident[0]!=source: continue
                if key not in result.get("selected_match_ids",[]): continue
                item={k:v for k,v in result.items() if k not in ("rows_by_match","matched_rows","selected_match_ids","rows")}
                rows=result.get("rows_by_match",{}).get(key,[])
                total=result.get("matched_rows",{}).get(key,0)
                item.update(rows=rows,matching_rows_observed=total,rows_truncated=max(0,total-len(rows)))
                versions.append(item)
            data[source+".json"]={"match_id":key,"source":source,"versions":versions,
                "coverage":"exact_manifest_only_not_all_history" if versions else "unplanned_or_unavailable",
                "silent_version_selection":False}
        manifest={"format":FORMAT,"match_id":key,"origin":"real_source_records",
            "selection":selection,"limits":asdict(limits),"other_match_linked_tables":inventory,
            "coverage_complete":False,"availability":"not_inferred_from_ingestion",
            "json_codec":"repository_json_value_v1_dates_uuid_decimal_as_strings_bytes_base64",
            "review":{"owner_public_sports_approval":"2026-11-25","content":"pending","redistribution_rights":"pending"}}
        output[key]=(data,manifest)
    return output


def sha(data): return hashlib.sha256(data).hexdigest()


def publish(root, packages, *, secrets=()):
    """Per-bundle atomic immutable publication; index is the commit marker, written last."""
    import fcntl
    root=Path(root)
    need(not root.is_symlink() and len(packages)<=24, "unsafe_output_path")
    prepared={}
    total=0
    for key,(data,manifest) in packages.items():
        need(mid(key)==key and set(data)==set(FILES),"bundle_invalid")
        inspect_content(data,secrets)
        inspect_content(manifest,secrets)
        encoded={name:dumps(value) for name,value in data.items()}
        manifest=dict(manifest)
        manifest["files"]={name:sha(value) for name,value in encoded.items()}
        manifest["bundle_digest"]=sha(dumps(manifest))
        encoded["manifest.json"]=dumps(manifest)
        total+=sum(len(v) for v in encoded.values())
        need(total<=manifest["limits"]["output_bytes"],"output_limit")
        prepared[key]=encoded
    root.mkdir(parents=True,exist_ok=True)
    samples=root/"samples"
    need(not samples.is_symlink() and not (root/".export.lock").is_symlink(), "unsafe_output_path")
    samples.mkdir(exist_ok=True)
    with (root/".export.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        index_path=root/"index.json"
        need(not index_path.is_symlink(), "unsafe_output_path")
        index=read_json(index_path,1024*1024) if index_path.exists() else {"catalog_version":2,"entries":[],"real_examples":[]}
        need(type(index)is dict and type(index.get("real_examples",[]))is list,"index_invalid")
        inspect_content(index,secrets)
        staging=Path(tempfile.mkdtemp(prefix=".export-stage-",dir=root))
        try:
            entries={entry["match_id"]:entry for entry in index.get("real_examples",[])}
            for key,encoded in sorted(prepared.items()):
                target=samples/("match_"+key)
                entry={"match_id":key,"origin":"real_source_records","path":"samples/match_"+key,
                       "manifest_sha256":sha(encoded["manifest.json"]),"review_state":"content_and_rights_review_pending"}
                if target.exists():
                    need(not target.is_symlink() and target.is_dir(),"unsafe_output_path")
                    need({p.name for p in target.iterdir()}==set(encoded),"immutable_bundle_conflict")
                    need(all((target/name).is_file() and not (target/name).is_symlink() and
                             (target/name).stat().st_size==len(payload) and (target/name).read_bytes()==payload for name,payload in encoded.items()),"immutable_bundle_conflict")
                else:
                    directory=staging/("match_"+key)
                    directory.mkdir(mode=0o700)
                    for name,payload in encoded.items():
                        with (directory/name).open("xb") as stream:
                            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
                    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                    os.rename(directory,target)
                need(key not in entries or entries[key]==entry,"index_conflict")
                entries[key]=entry
            index["catalog_version"]=2
            index["real_examples"]=[entries[key] for key in sorted(entries)]
            index["owner_public_sports_approval"]="2026-11-25"
            index["publication_status"]="owner_approved_real_sports_content_and_rights_review_required"
            index.pop("projection_policy",None)
            index["projection_policies"]={"synthetic":"linked-sanitized-v1","real_examples":"real-sports-original-v1"}
            inspect_content(index,secrets)
            encoded_index=dumps(index)
            need(len(encoded_index)<=1024*1024 and total+len(encoded_index)<=
                 min((item[1]["limits"]["output_bytes"] for item in packages.values()),default=128*1024*1024),"output_limit")
            with (staging/"index.json").open("xb") as stream:
                stream.write(encoded_index); stream.flush(); os.fsync(stream.fileno())
            os.replace(staging/"index.json",index_path)
            for directory in (samples,root):
                fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
        finally:
            shutil.rmtree(staging)
    return {"format":FORMAT,"published_matches":len(prepared),"automatic_git_push":False}


def reuse(root, ids=None):
    root=Path(root)
    need(not root.is_symlink() and not (root/"samples").is_symlink() and
         not (root/"index.json").is_symlink(),"unsafe_output_path")
    index=read_json(root/"index.json",1024*1024)
    entries=index.get("real_examples",[])
    need(type(entries)is list,"index_invalid")
    requested=set(mid(x) for x in ids) if ids else {e["match_id"] for e in entries}
    need(len(requested)<=24,"match_limit")
    result={}
    total_bytes=0
    for entry in entries:
        key=mid(entry["match_id"])
        if key not in requested: continue
        need(entry.get("path")=="samples/match_"+key,"index_invalid")
        directory=root/entry["path"]
        need(not directory.is_symlink(),"unsafe_output_path")
        manifest_file=directory/"manifest.json"
        need(not manifest_file.is_symlink() and manifest_file.stat().st_size<=1024*1024,"file_limit")
        manifest_bytes=manifest_file.read_bytes()
        need(len(manifest_bytes)<=1024*1024 and sha(manifest_bytes)==entry["manifest_sha256"],"manifest_changed")
        manifest=json.loads(manifest_bytes,object_pairs_hook=unique)
        digest=manifest.pop("bundle_digest",None)
        need(digest==sha(dumps(manifest)) and manifest.get("format")==FORMAT and
             manifest.get("match_id")==key and set(manifest.get("files",{}))==set(FILES),"manifest_changed")
        values={}
        for name in FILES:
            file=directory/name
            need(not file.is_symlink() and file.stat().st_size<=128*1024*1024,"file_limit")
            total_bytes+=file.stat().st_size
            need(total_bytes<=256*1024*1024,"file_limit")
            raw=file.read_bytes()
            need(sha(raw)==manifest["files"][name],"sample_changed")
            values[name]=json.loads(raw,object_pairs_hook=unique)
        manifest["bundle_digest"]=digest
        inspect_content(values)
        inspect_content(manifest)
        need(key not in result,"index_invalid")
        result[key]={"manifest":manifest,"records":values}
    need(set(result)==requested,"sample_not_found")
    return result


@contextmanager
def bounded_process(seconds):
    import resource
    previous=resource.getrlimit(resource.RLIMIT_AS)
    old=signal.getsignal(signal.SIGALRM)
    alarm=signal.alarm(0)
    start=time.monotonic()
    cap=min(previous[0],4*1024**3) if previous[0]!=resource.RLIM_INFINITY else 4*1024**3
    try:
        resource.setrlimit(resource.RLIMIT_AS,(cap,previous[1]))
        signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(ExportError("deadline")))
        signal.alarm(min(seconds,alarm) if alarm else seconds)
        yield
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM,old)
        resource.setrlimit(resource.RLIMIT_AS,previous)
        if alarm: signal.alarm(max(1,alarm-int(time.monotonic()-start)))


def export(connection,client,bucket,ids,plan,output,limits,selection,secrets=()):
    budget=Budget(limits)
    need(0<len(ids)<=limits.matches and len(set(ids))==len(ids) and all(mid(x)==x for x in ids),"match_limit")
    pg,schema,inventory=read_pg(connection,ids,limits)
    budget.check()
    planned=object_plan(plan,ids,limits)
    reader=S3Reader(client,bucket,planned,budget)
    results={}
    for ident,entry in sorted(planned.items(),key=lambda kv:(kv[0][0],kv[0][1],kv[0][2] or "")):
        result=reader.read(ident,entry)
        result["selected_match_ids"]=sorted(entry["match_ids"])
        results[ident]=result
    packages=bundles(ids,pg,schema,inventory,results,selection,limits)
    budget.check()
    return publish(output,packages,secrets=secrets)


class Parser(argparse.ArgumentParser):
    def error(self,message): raise ExportError("invalid_arguments")


def main(argv=None):
    logging.disable(logging.CRITICAL)
    parser=Parser(description=__doc__)
    parser.add_argument("--match-id",action="append",default=[])
    for flag in ("date-from","date-to","player","tournament","env-file","source-manifest","reuse"):
        parser.add_argument("--"+flag)
    parser.add_argument("--derive-current-parts",action="store_true")
    parser.add_argument("--output",default=str(ROOT/"docs/ml/data_catalog"))
    for name,value in asdict(Limits()).items():
        parser.add_argument("--max-"+name.replace("_","-"),default=str(value))
    db=None
    try:
        args=parser.parse_args(argv)
        if args.reuse:
            need(not any((args.date_from,args.date_to,args.player,args.tournament,args.env_file,
                          args.source_manifest,args.derive_current_parts)),"reuse_arguments_conflict")
            result=reuse(args.reuse,args.match_id)
            print(json.dumps({"format":FORMAT,"reused_matches":len(result),"production_access":False}))
            return 0
        limits=Limits(**{k:int(getattr(args,"max_"+k)) for k in asdict(Limits())})
        limits.validate()
        os.environ["OPENBLAS_NUM_THREADS"]="1"
        with bounded_process(limits.seconds):
            values=config(args.env_file)
            import boto3
            import psycopg2
            from botocore.config import Config
            db=psycopg2.connect(host=values["POSTGRES_HOST"],port=int(values["POSTGRES_PORT"]),
                user=values["POSTGRES_USER"],password=values["POSTGRES_PASSWORD"],dbname=values["POSTGRES_DB"],
                connect_timeout=3,options="-c default_transaction_read_only=on -c statement_timeout=5000 "
                "-c lock_timeout=1000 -c timezone=UTC")
            db.set_session(readonly=True,autocommit=False,isolation_level="REPEATABLE READ")
            ids,selection=select_ids(db,args.match_id,limits,date_from=args.date_from,date_to=args.date_to,
                                    player=args.player,tournament=args.tournament)
            need(ids,"selection_empty")
            need(not(args.source_manifest and args.derive_current_parts),"manifest_ambiguous")
            if args.source_manifest:
                plan=read_json(args.source_manifest,1024*1024)
            elif args.derive_current_parts:
                rows,_,_=read_pg(db,ids,limits)
                plan={"manifest_version":1,"matches":{}}
                for key,row in rows.items():
                    mapping=related_partition_keys(row.get("pbp_s3_path"))
                    if mapping:
                        suffix=mapping["pbp/"][len("pbp/"):]
                        plan["matches"][key]={s:[{"key":s+"/"+suffix,"current":True}] for s in SOURCES}
                selection["object_selection"]="explicit_current_sibling_candidates_not_all_history"
            else:
                plan={"manifest_version":1,"matches":{}}
                selection["object_selection"]="unplanned_sources_explicitly_missing"
            client=boto3.client("s3",endpoint_url=values["S3_ENDPOINT_URL"],region_name=values["S3_REGION"],
                aws_access_key_id=values["S3_ACCESS_KEY_ID"],aws_secret_access_key=values["S3_SECRET_ACCESS_KEY"],
                config=Config(connect_timeout=2,read_timeout=3,retries={"total_max_attempts":1,"mode":"standard"},
                              s3={"addressing_style":"path"}))
            summary=export(db,client,values["S3_BUCKET"],ids,plan,args.output,limits,selection,
                           secrets=(values["POSTGRES_PASSWORD"],values["S3_ACCESS_KEY_ID"],values["S3_SECRET_ACCESS_KEY"]))
        print(json.dumps(summary))
        return 0
    except Exception:
        print('{"error":"match_sample_export_failed","data_printed":false}',file=sys.stderr)
        return 1
    finally:
        if db is not None:
            try: db.rollback(); db.close()
            except Exception: pass


if __name__=="__main__":
    raise SystemExit(main())
