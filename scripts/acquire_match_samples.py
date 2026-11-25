"""Bounded first-20 selection and local batches; never dispatched on import."""
import argparse
from dataclasses import replace
from datetime import date
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import export_match_samples as e

TARGET = 20
PAGE_SIZE = 128
MAX_PAGES = 4
ARCHIVE_LIMIT = 64*1024*1024
PLAN_FORMAT = "first-20-tour-challenger-v1"


def validate_plan(plan):
    e.need(type(plan) is dict and plan.get("format") == PLAN_FORMAT, "selection_plan_invalid")
    ids = plan.get("selected_ids")
    e.need(type(ids) is list and 0 < len(ids) <= TARGET and len(set(ids)) == len(ids)
           and all(e.mid(key) == key for key in ids), "selection_plan_invalid")
    e.need(plan.get("target") == TARGET and plan.get("shortfall") == TARGET-len(ids) and
           type(plan.get("discovery")) is dict, "selection_plan_invalid")
    e.inspect_content(plan)
    e.need(len(e.dumps(plan)) <= 1024*1024, "selection_plan_limit")
    return plan


def check_candidate_plan(raw):
    """A LIMIT above a plain PK scan stops after the bounded prefix, no filtering."""
    e.need(type(raw) is list and len(raw) == 1, "candidate_plan_invalid")
    root = raw[0]["Plan"]
    e.need(root.get("Total Cost", float("inf")) <= 10000, "candidate_plan_expensive")
    nodes, scans = [root], []
    while nodes:
        node = nodes.pop()
        kind = node.get("Node Type")
        e.need(kind in ("Limit", "Index Scan", "Index Only Scan"), "candidate_plan_unbounded")
        e.need(not any(k in node for k in ("Filter", "Join Filter", "One-Time Filter")), "candidate_plan_unbounded")
        if kind in ("Index Scan", "Index Only Scan"):
            e.need(node.get("Relation Name") == "matches" and
                   node.get("Index Name") == "matches_pkey", "candidate_index_required")
            scans.append(node)
        nodes.extend(node.get("Plans", []))
    e.need(len(scans) == 1, "candidate_plan_invalid")


def candidate_pool(db, *, pages=MAX_PAGES, after=None):
    e.need(type(pages) is int and 1 <= pages <= MAX_PAGES, "candidate_page_limit")
    cursor = e.mid(after) if after else None
    e.check_session(db)
    pool, observed, exhausted = [], 0, False
    for _ in range(pages):
        where = " WHERE match_id > %s::uuid" if cursor else ""
        params = (cursor, PAGE_SIZE+1) if cursor else (PAGE_SIZE+1,)
        query = ("SELECT match_id::text, gender, level, match_date::text, left(surface,64), "
                 "status, details_version, has_pbp, left(pbp_s3_path,1025), duplicate_of::text "
                 "FROM public.matches"+where+" ORDER BY match_id LIMIT %s")
        with db.cursor() as c:
            c.execute("EXPLAIN (FORMAT JSON) "+query, params)
            plans = c.fetchmany(2)
            e.need(len(plans) == 1, "candidate_plan_invalid")
            check_candidate_plan(plans[0][0])
            c.execute(query, params)
            rows = c.fetchmany(PAGE_SIZE+2)
        e.need(len(rows) <= PAGE_SIZE+1, "candidate_row_limit")
        accepted = rows[:PAGE_SIZE]
        previous = cursor
        for row in accepted:
            e.need(len(row) == 10, "candidate_schema")
            key = e.mid(row[0])
            e.need(previous is None or key > previous, "candidate_order")
            previous = key
            observed += 1
            if row[2] not in ("tour", "challenger") or row[9] is not None:
                continue
            year = date.fromisoformat(row[3]).year if row[3] else None
            path = row[8]
            # Never publish a truncated path as evidence.
            if path is not None and len(path) > 1024:
                path = None
            pool.append({"match_id":key, "gender":row[1], "level":row[2], "year":year,
                         "surface":row[4], "status":row[5], "details_version":row[6],
                         "has_pbp_flag":row[7], "pbp_s3_path":path})
        if len(rows) <= PAGE_SIZE:
            exhausted = True
            break
        e.need(accepted, "candidate_order")
        cursor = e.mid(accepted[-1][0])
    e.inspect_content(pool)
    return pool, {"method":"bounded_primary_key_prefix", "start_after":after,
                  "max_pages":pages, "page_size":PAGE_SIZE, "rows_examined":observed,
                  "eligible_rows":len(pool), "prefix_exhausted":exhausted,
                  "population_complete":False, "next_after":cursor}


def diversity_features(row):
    return {(field, str(row.get(field))) for field in
            ("gender", "level", "year", "surface", "status", "details_version", "has_pbp_flag")} | {
                ("path_availability", str(bool(e.related_partition_keys(row.get("pbp_s3_path")))))}


def choose(pool, discovery):
    remaining = {row["match_id"]:row for row in pool}
    e.need(len(remaining) == len(pool), "candidate_duplicate")
    chosen, covered = [], set()
    while remaining and len(chosen) < TARGET:
        # Stable UUID breaks ties. Coverage diversity does not assert score semantics.
        key = min(remaining, key=lambda key:(-len(diversity_features(remaining[key])-covered), key))
        row = remaining.pop(key)
        chosen.append(row)
        covered |= diversity_features(row)
    e.need(chosen, "no_eligible_matches_in_bounded_pool")
    return validate_plan({"format":PLAN_FORMAT, "target":TARGET,
        "selected_ids":[row["match_id"] for row in chosen], "selected_metadata":chosen,
        "shortfall":TARGET-len(chosen), "discovery":discovery,
        "score_formats":"unknown_until_source_review", "complete_population":False})


def source_manifest(rows, ids, supplemental=None):
    """Writer-supported exact siblings only; never LIST or infer historical names."""
    result = {"manifest_version":1, "matches":{}}
    missing = []
    for key in ids:
        mapping = e.related_partition_keys(rows.get(key, {}).get("pbp_s3_path"))
        if not mapping:
            missing.append(key)
            result["matches"][key] = {}
            continue
        suffix = mapping["pbp/"][len("pbp/"):]
        result["matches"][key] = {source:[{"key":source+"/"+suffix, "current":True}]
                                  for source in e.SOURCES}
    if supplemental is not None:
        # Exact reviewed historical paths/versions may replace/extend current candidates.
        e.object_plan(supplemental, ids, e.Limits(matches=TARGET,objects=100))
        for key, sources in supplemental["matches"].items():
            for source, versions in sources.items():
                result["matches"].setdefault(key,{})[source] = versions
    return result, missing


def acquire(db, client, bucket, output, *, plan=None, supplemental=None,
            offset=0, batch_size=2, pages=MAX_PAGES, after=None, secrets=(), limits=None):
    e.need(type(offset) is int and 0 <= offset < TARGET and
           type(batch_size) is int and 1 <= batch_size <= 5, "batch_invalid")
    limits = limits or e.Limits(matches=5,objects=100,total_bytes=128*1024*1024,seconds=240)
    limits.validate()
    budget = e.Budget(limits)
    if plan is None:
        e.need(offset == 0, "resume_requires_selection_plan")
        pool, discovery = candidate_pool(db,pages=pages,after=after)
        plan = choose(pool,discovery)
    else:
        e.need(after is None, "resume_selection_ambiguous")
        plan = validate_plan(plan)
    ids = plan["selected_ids"][offset:offset+batch_size]
    e.need(ids, "batch_empty")
    pg, schema, inventory = e.read_pg(db,ids,limits)
    e.need(all(row.get("level") in ("tour","challenger") and row.get("duplicate_of") is None
               for row in pg.values()),"selected_scope_changed")
    if supplemental is not None:
        e.object_plan(supplemental,plan["selected_ids"],e.Limits(matches=TARGET,objects=100))
        supplemental = {"manifest_version":1,"matches":{key:value for key,value in
                        supplemental["matches"].items() if key in ids}}
    raw_plan, no_paths = source_manifest(pg,ids,supplemental)
    planned = e.object_plan(raw_plan,ids,limits)
    reader = e.S3Reader(client,bucket,planned,budget)
    results = {}
    for ident, entry in sorted(planned.items(),key=lambda kv:(kv[0][0],kv[0][1],kv[0][2] or "")):
        result = reader.read(ident,entry)
        result["selected_match_ids"] = sorted(entry["match_ids"])
        results[ident] = result
    selection = {"mode":"first20_bounded_diverse", "plan_sha256":e.sha(e.dumps(plan)),
                 "batch_offset":offset, "batch_size":len(ids), "target":TARGET,
                 "selection_shortfall":plan["shortfall"]}
    packages = e.bundles(ids,pg,schema,inventory,results,selection,limits)
    summary = {"format":"first20-acquisition-report-v1", "target":TARGET,
               "selected_count":len(plan["selected_ids"]), "selection_shortfall":plan["shortfall"],
               "exported_count":len(ids), "batch_offset":offset, "next_offset":offset+len(ids),
               "plan_sha256":selection["plan_sha256"], "source_commit":None,
               "current_pointer_unavailable_match_ids":no_paths, "sources":{}, "score_formats":"unverified",
               "other_linked_pg_tables":inventory,
               "blockers":["unindexed_or_unknown_other_pg_links_not_exported",
                           "exact_sibling_candidates_do_not_discover_all_history",
                           "content_and_source_redistribution_rights_review_required"]}
    for key, (data, _) in packages.items():
        summary["sources"][key] = {}
        for source in e.SOURCES:
            values = data[source+".json"]
            versions = values["versions"]
            summary["sources"][key][source] = {
                "coverage":values["coverage"], "objects":len(versions),
                "rows":sum(len(v["rows"]) for v in versions),
                "truncated_rows":sum(v["rows_truncated"] for v in versions),
                "object_states":[v["coverage"] for v in versions],
                "schema_variants":[v.get("schema",[]) for v in versions]}
    # Inspect control files before publishing any batch.
    e.inspect_content(plan,secrets)
    e.inspect_content(summary,secrets)
    budget.check()
    e.publish(output,packages,secrets=secrets)
    return plan, summary


def make_archive(catalog, plan, report):
    """Only indexed, verified files; no directory sweep or configuration inclusion."""
    e.reuse(catalog)
    index = e.read_json(Path(catalog)/"index.json",1024*1024)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream,"w",compression=zipfile.ZIP_STORED) as z:
        def add(name,data):
            e.need(stream.tell()+len(data)+4096 <= ARCHIVE_LIMIT,"delivery_limit")
            info = zipfile.ZipInfo(name, date_time=(1980,1,1,0,0,0))
            info.external_attr = 0o100600 << 16
            z.writestr(info,data)
        add("selection.json",e.dumps(plan))
        add("acquisition-report.json",e.dumps(report))
        add("catalog/index.json",(Path(catalog)/"index.json").read_bytes())
        for entry in index["real_examples"]:
            for name in (*e.FILES,"manifest.json"):
                path = entry["path"]+"/"+name
                add("catalog/"+path,(Path(catalog)/path).read_bytes())
    payload = stream.getvalue()
    e.need(len(payload) <= ARCHIVE_LIMIT,"delivery_limit")
    return payload


def run(request, project, reviewed_sha):
    """Called only by streamed reviewed bootstrap; writes archive bytes to a pipe."""
    import os
    import importlib.metadata
    e.need(os.geteuid() != 0,"nonroot_required")
    os.environ["OPENBLAS_NUM_THREADS"]="1"
    os.environ["OMP_NUM_THREADS"]="1"
    e.need(sys.version_info >= (3,11),"python_version")
    # Import dependencies only; never import storage.db or ingestion jobs.
    runtime = {"python":".".join(map(str,sys.version_info[:3])),"packages":{}}
    for package, minimum in (("psycopg2-binary",(2,9)),("boto3",(1,35)),("pyarrow",(15,0))):
        import re
        version = importlib.metadata.version(package)
        runtime["packages"][package] = version
        parts = re.match(r"([0-9]+)\.([0-9]+)",version)
        e.need(parts is not None and tuple(map(int,parts.groups())) >= minimum,"dependency_version")
    import boto3
    import psycopg2
    from botocore.config import Config
    values = e.config(Path(project)/".env")
    db = None
    try:
        db = psycopg2.connect(host=values["POSTGRES_HOST"],port=int(values["POSTGRES_PORT"]),
            user=values["POSTGRES_USER"],password=values["POSTGRES_PASSWORD"],dbname=values["POSTGRES_DB"],
            connect_timeout=3,options="-c default_transaction_read_only=on -c statement_timeout=5000 "
            "-c lock_timeout=1000 -c timezone=UTC")
        db.set_session(readonly=True,autocommit=False,isolation_level="REPEATABLE READ")
        e.check_session(db)
        with db.cursor() as c:
            c.execute("SELECT current_setting('server_version_num') LIMIT 1")
            version_rows = c.fetchmany(2)
        e.need(len(version_rows)==1,"postgres_version")
        runtime["postgres_major"] = int(version_rows[0][0])//10000
        client = boto3.client("s3",endpoint_url=values["S3_ENDPOINT_URL"],region_name=values["S3_REGION"],
            aws_access_key_id=values["S3_ACCESS_KEY_ID"],aws_secret_access_key=values["S3_SECRET_ACCESS_KEY"],
            config=Config(connect_timeout=2,read_timeout=3,retries={"total_max_attempts":1},
                          s3={"addressing_style":"path"}))
        with e.bounded_process(240), tempfile.TemporaryDirectory(prefix="tennis-samples-") as temp:
            plan, report = acquire(db,client,values["S3_BUCKET"],Path(temp)/"catalog",
                plan=request.get("plan"),supplemental=request.get("supplemental"),
                offset=request["offset"],batch_size=request["batch_size"],pages=request["pages"],
                after=request.get("after"),secrets=(values["POSTGRES_PASSWORD"],
                    values["S3_ACCESS_KEY_ID"],values["S3_SECRET_ACCESS_KEY"]))
            report["source_commit"] = reviewed_sha
            report["runtime"] = runtime
            e.inspect_content(report)
            payload = make_archive(Path(temp)/"catalog",plan,report)
        sys.stdout.buffer.write(payload)
    finally:
        if db is not None:
            db.rollback()
            db.close()
