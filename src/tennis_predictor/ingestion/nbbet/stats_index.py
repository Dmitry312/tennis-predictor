"""Private incremental statistics row index; independent of PBP filenames."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

@contextmanager
def index(bucket):
    root=Path(os.environ.get("TENNIS_STATS_INDEX_DIR","/var/tmp/tennis-stats-crosscheck-index"))
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if root.is_symlink() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:
        raise RuntimeError("unsafe statistics index directory")
    path=root/(hashlib.sha256(bucket.encode()).hexdigest()+".sqlite")
    if path.is_symlink(): raise RuntimeError("unsafe statistics index file")
    conn=sqlite3.connect(path,timeout=30)
    try:
        conn.executescript("""
          PRAGMA cache_size=-8192;
          CREATE TABLE IF NOT EXISTS objects(prefix TEXT,key TEXT PRIMARY KEY,etag TEXT,size INTEGER);
          CREATE TABLE IF NOT EXISTS rows(key TEXT,ordinal INTEGER,mid TEXT,scope INTEGER,stamp TEXT,payload TEXT,
                                        PRIMARY KEY(key,ordinal));
          CREATE INDEX IF NOT EXISTS match_scope ON rows(mid,scope,stamp);
        """)
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally: conn.close()

def lookup(conn,prefix,objects,wanted,read,latest):
    cached={k:(tag,size) for k,tag,size in conn.execute(
        "SELECT key,etag,size FROM objects WHERE prefix=?",(prefix,))}
    for key in sorted(set(cached)-set(objects)):
        conn.execute("DELETE FROM rows WHERE key=?",(key,))
        conn.execute("DELETE FROM objects WHERE key=?",(key,))
    for key,(etag,size) in sorted(objects.items()):
        if cached.get(key)==(etag,size): continue
        conn.execute("DELETE FROM rows WHERE key=?",(key,))
        for i,row in enumerate(read(key,etag)):
            mid=str(row["canonical_match_id"]); scope=int(row["scope"])
            payload=json.dumps(row,default=str,sort_keys=True,allow_nan=False)
            conn.execute("INSERT INTO rows VALUES (?,?,?,?,?,?)",
                         (key,i,mid,scope,str(row.get("fetched_at") or ""),payload))
        conn.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?,?)",(prefix,key,etag,size))
    # Query only requested matches; never materialize a whole year's rows.
    def selected():
        for mid in sorted(wanted):
            for payload, in conn.execute("""SELECT r.payload FROM rows r JOIN objects o ON o.key=r.key
                         WHERE o.prefix=? AND r.mid=? ORDER BY r.stamp,r.key,r.ordinal""",(prefix,mid)):
                yield json.loads(payload)
    return latest(selected())
