"""Bounded read-only ML dataset quality audit. No application imports or writes."""
import datetime as dt
import json
import sys
import importlib.util

# Standalone: the script is streamed from stdin and imports no application modules.
import os
import re
from pathlib import Path

MAX_OUTPUT = 16384
FIELDS = ('POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB')
STATUSES = tuple(range(1, 11))
YEARS_LIMIT = 40

class AuditFailure(Exception):
    pass

def settings():
    try:
        with Path('/root/Tennis_pelmen_model/.env').open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536 or bytes((0,)) in raw:
            raise AuditFailure('connection_config_unavailable')
        text = raw.decode('utf-8')
        found = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.fullmatch(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
            if not m or m[1] not in FIELDS:
                continue
            if m[1] in found:
                raise AuditFailure('connection_config_unavailable')
            val = m[2].strip()
            if val.startswith(('"', "'")):
                if len(val) < 2 or val[-1] != val[0]:
                    raise AuditFailure('connection_config_unavailable')
                val = val[1:-1]
            elif not re.fullmatch(r'[^\s#\x22\x27\\]*', val):
                raise AuditFailure('connection_config_unavailable')
            if '\\' in val or '${' in val:
                raise AuditFailure('connection_config_unavailable')
            found[m[1]] = val
        vals = {k: os.environ.get(k, found.get(k)) for k in FIELDS}
        if any(type(v) is not str or not v or len(v) > 4096 or
               any(c in v for c in ('\r', '\n', '\x00')) or '${' in v for v in vals.values()):
            raise AuditFailure('connection_config_unavailable')
        if not re.fullmatch(r'[0-9]{1,5}', vals['POSTGRES_PORT']):
            raise AuditFailure('connection_config_unavailable')
        port = int(vals['POSTGRES_PORT'])
        if not 1 <= port <= 65535:
            raise AuditFailure('connection_config_unavailable')
        return vals, port
    except FileNotFoundError:
        vals = {k: os.environ.get(k) for k in FIELDS}
        if any(type(v) is not str or not v or len(v) > 4096 or
               any(c in v for c in ('\\r', '\\n', '\\x00')) or '\${' in v for v in vals.values()):
            raise AuditFailure('connection_config_unavailable')
        if not re.fullmatch(r'[0-9]{1,5}', vals['POSTGRES_PORT']):
            raise AuditFailure('connection_config_unavailable')
        port = int(vals['POSTGRES_PORT'])
        if not 1 <= port <= 65535:
            raise AuditFailure('connection_config_unavailable')
        return vals, port
    except (OSError, UnicodeError):
        raise AuditFailure('connection_config_unavailable') from None

# Fixed query inventory; no user-controlled SQL or identifiers.
QUERIES = {
 'session': "SELECT current_setting(%s), current_setting(%s), CURRENT_TIMESTAMP AT TIME ZONE %s LIMIT %s",
 'schema': """SELECT a.attname, t.typname FROM pg_catalog.pg_attribute a
 JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
 WHERE n.nspname=%s AND c.relname=%s AND a.attname=ANY(%s)
 AND a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p')
 LIMIT %s""",
 'totals': """SELECT COUNT(*),
 COUNT(*) FILTER (WHERE duplicate_of IS NULL),
 COUNT(*) FILTER (WHERE duplicate_of IS NOT NULL),
 COUNT(*) FILTER (WHERE match_date IS NULL),
 COUNT(*) FILTER (WHERE match_date > %s),
 COUNT(*) FILTER (WHERE player1 IS NULL OR btrim(player1)='' OR player2 IS NULL OR btrim(player2)=''),
 COUNT(*) FILTER (WHERE player1 IS NOT NULL AND player2 IS NOT NULL AND lower(btrim(player1))=lower(btrim(player2))),
 COUNT(*) FILTER (WHERE gender IS NULL OR gender NOT IN ('M','F')),
 COUNT(*) FILTER (WHERE level IS NULL OR level NOT IN ('tour','challenger','itf','exhibition')),
 COUNT(*) FILTER (WHERE status IS NULL OR status <> ALL(%s)),
 COUNT(*) FILTER (WHERE duplicate_of=match_id),
 COUNT(*) FILTER (WHERE duplicate_of IS NOT NULL AND duplicate_of NOT IN (SELECT match_id FROM public.matches)),
 COUNT(*) FILTER (WHERE has_pbp IS TRUE),
 COUNT(*) FILTER (WHERE pbp_s3_path IS NOT NULL),
 COUNT(*) FILTER (WHERE has_pbp IS TRUE AND pbp_s3_path IS NULL),
 COUNT(*) FILTER (WHERE has_pbp IS FALSE AND pbp_s3_path IS NOT NULL),
 COUNT(*) FILTER (WHERE pbp_completeness IS NOT NULL AND (pbp_completeness < 0 OR pbp_completeness > 1 OR pbp_completeness='NaN'::float8)),
 COUNT(*) FILTER (WHERE (odds_open_p1 IS NULL) <> (odds_open_p2 IS NULL)),
 COUNT(*) FILTER (WHERE odds_open_p1 IS NOT NULL AND odds_open_p2 IS NOT NULL),
 COUNT(*) FILTER (WHERE odds_open_p1 IS NOT NULL AND odds_open_p2 IS NOT NULL AND (odds_open_p1 <= 1 OR odds_open_p2 <= 1)),
 COUNT(*) FILTER (WHERE odds_open_p1 IS NOT NULL AND odds_open_p2 IS NOT NULL AND odds_open_captured_at IS NULL),
 COUNT(*) FILTER (WHERE odds_open_captured_at IS NOT NULL AND start_ts IS NOT NULL AND odds_open_captured_at >= start_ts),
 COUNT(*) FILTER (WHERE odds_open_p1 IS NOT NULL AND odds_open_p2 IS NOT NULL AND start_ts IS NULL),
 COUNT(*) FILTER (WHERE odds_open_p1 IS NOT NULL AND odds_open_p2 IS NOT NULL AND odds_open_captured_at < start_ts AND duplicate_of IS NULL AND status=3)
 FROM public.matches m LIMIT %s""",
 'years': """SELECT EXTRACT(YEAR FROM match_date)::int, COUNT(*),
 COUNT(*) FILTER (WHERE duplicate_of IS NULL),
 COUNT(*) FILTER (WHERE status=3 AND duplicate_of IS NULL),
 COUNT(*) FILTER (WHERE has_pbp IS TRUE AND duplicate_of IS NULL),
 COUNT(*) FILTER (WHERE odds_open_p1 IS NOT NULL AND odds_open_p2 IS NOT NULL AND duplicate_of IS NULL)
 FROM public.matches WHERE match_date IS NOT NULL
 GROUP BY 1 ORDER BY 1 LIMIT %s""",
 'categories': """SELECT
 COUNT(*) FILTER (WHERE status=3),
 COUNT(*) FILTER (WHERE status=9),
 COUNT(*) FILTER (WHERE status IN (5,8,10)),
 COUNT(*) FILTER (WHERE status IS NULL),
 COUNT(*) FILTER (WHERE gender='M'),
 COUNT(*) FILTER (WHERE gender='F'),
 COUNT(*) FILTER (WHERE surface IS NULL OR btrim(surface)=''),
 COUNT(*) FILTER (WHERE start_ts IS NULL),
 COUNT(*) FILTER (WHERE odds_open_source='listing'),
 COUNT(*) FILTER (WHERE odds_open_source='page'),
 COUNT(*) FILTER (WHERE odds_open_source IS NULL),
 COUNT(*) FILTER (WHERE odds_open_captured_at IS NOT NULL AND odds_open_status IN (2,3,9,10))
 FROM public.matches LIMIT %s"""
}
EXPECTED = {'match_id':'uuid','match_date':'date','player1':'varchar','player2':'varchar',
'gender':'varchar','level':'varchar','surface':'varchar','status':'int2','duplicate_of':'uuid',
'has_pbp':'bool','pbp_s3_path':'varchar','pbp_completeness':'float8',
'odds_open_p1':'numeric','odds_open_p2':'numeric','odds_open_captured_at':'timestamp',
'odds_open_source':'varchar','odds_open_status':'int2','start_ts':'timestamp'}

TOTAL_NAMES = ('matches_total','canonical','marked_duplicates','missing_date','future_date',
'missing_player','same_player','invalid_gender','invalid_level','unknown_status',
'self_duplicate','dangling_duplicate','has_pbp','pbp_paths','pbp_flag_without_path',
'pbp_path_without_flag','invalid_completeness','one_sided_odds','paired_odds',
'invalid_odds','odds_missing_capture_time','odds_at_or_after_start',
'odds_without_start','strict_prematch_odds_finished_canonical')
CATEGORY_NAMES = ('finished','retired','other_terminal','null_status','men','women',
'missing_surface','missing_start','odds_listing','odds_page','odds_source_null','odds_capture_live_or_final')

def count(v):
    if type(v) is not int or not 0 <= v <= 10**12:
        raise AuditFailure('unexpected_result')
    return v

def fetch(cursor, name, params, limit):
    cursor.execute(QUERIES[name], params)
    result = cursor.fetchmany(limit + 1)
    if len(result) > limit:
        raise AuditFailure('result_limit')
    return result

def audit(conn):
    with conn.cursor() as cur:
        row = fetch(cur, 'session', ('transaction_read_only','transaction_isolation','UTC',1),1)
        if len(row)!=1 or row[0][:2]!=('on','repeatable read') or type(row[0][2]) is not dt.datetime:
            raise AuditFailure('readonly_not_confirmed')
        now=row[0][2]
        schema=fetch(cur,'schema',('public','matches',list(EXPECTED),len(EXPECTED)),len(EXPECTED))
        if dict(schema)!=EXPECTED:
            raise AuditFailure('schema_mismatch')
        total=fetch(cur,'totals',(now.date(),list(STATUSES),1),1)
        if len(total)!=1 or len(total[0])!=len(TOTAL_NAMES):
            raise AuditFailure('unexpected_result')
        totals={k:count(v) for k,v in zip(TOTAL_NAMES,total[0])}
        cats=fetch(cur,'categories',(1,),1)
        if len(cats)!=1 or len(cats[0])!=len(CATEGORY_NAMES):
            raise AuditFailure('unexpected_result')
        categories={k:count(v) for k,v in zip(CATEGORY_NAMES,cats[0])}
        years=[]
        for year,*vals in fetch(cur,'years',(YEARS_LIMIT+1,),YEARS_LIMIT):
            if type(year) is not int or not 1970<=year<=2100 or len(vals)!=5:
                raise AuditFailure('unexpected_result')
            years.append({'year':year,'matches':count(vals[0]),'canonical':count(vals[1]),
                          'finished_canonical':count(vals[2]),'pbp_canonical':count(vals[3]),
                          'paired_odds_canonical':count(vals[4])})
        if sum(x['matches'] for x in years)>totals['matches_total']:
            raise AuditFailure('unexpected_result')
        return {'audit_utc':now.isoformat()+'Z','complete':True,'totals':totals,
                'categories':categories,'years':years,
                'training_readiness':{'status':'unknown','reason':'labels_and_feature_provenance_not_verified'},
                'limitations':['aggregate_db_only','no_parquet_content_validation',
                               'candidate_duplicates_not_scanned','no_feature_lineage_validation']}

def serialize(report):
    data=json.dumps(report,ensure_ascii=True,allow_nan=False,separators=(',',':'))
    if len(data.encode('utf-8'))>MAX_OUTPUT:
        raise AuditFailure('output_limit')
    return data

def main():
    conn=None
    try:
        if importlib.util.find_spec('psycopg2') is None:
            raise AuditFailure('dependency_unavailable')
        import psycopg2
        vals,port=settings()
        conn=psycopg2.connect(host=vals['POSTGRES_HOST'],port=port,user=vals['POSTGRES_USER'],
            password=vals['POSTGRES_PASSWORD'],dbname=vals['POSTGRES_DB'],connect_timeout=5,
            application_name='production-ml-quality-audit',
            options='-c default_transaction_read_only=on -c statement_timeout=5000 '
                    '-c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000 '
                    '-c search_path=pg_catalog -c timezone=UTC')
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ',autocommit=False)
        print(serialize(audit(conn)))
        return 0
    except AuditFailure as exc:
        allowed={'connection_config_unavailable','readonly_not_confirmed','schema_mismatch',
                 'unexpected_result','result_limit','output_limit','dependency_unavailable'}
        code=exc.args[0] if exc.args and exc.args[0] in allowed else 'audit_failed'
    except Exception as exc:
        code={'57014':'query_timeout','55P03':'lock_timeout','42P01':'table_unavailable',
              '42703':'schema_mismatch'}.get(getattr(exc,'pgcode',None),'connection_or_query_failed')
    finally:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
            try: conn.close()
            except Exception: pass
    print(json.dumps({'audit_status':code}))
    return 1

if __name__=='__main__':
    sys.exit(main())
