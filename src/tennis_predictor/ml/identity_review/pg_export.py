"""Standalone read-only PostgreSQL transport. This file also runs over SSH stdin."""

import json
import re
import sys
from contextlib import closing
from io import StringIO
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID

PAGE_SIZE = 512
MAX_PAGES = 20_000
MAX_SECONDS = 25 * 60
FIELDS = (
    "match_id",
    "gender",
    "level",
    "match_date",
    "start_ts",
    "tournament_name",
    "player1",
    "player2",
    "nbbet_match_id",
    "championat_match_id",
    "nbbet_slug",
    "status",
    "duplicate_of",
    "details_version",
)


def iso(value):
    return (
        value.isoformat()
        if hasattr(value, "isoformat")
        else (str(value) if isinstance(value, UUID) else value)
    )


def validate_plan(raw):
    if not isinstance(raw, list) or len(raw) != 1:
        raise ValueError("PostgreSQL extraction requires one bounded index plan")
    root = raw[0]["Plan"]
    scans = root.get("Plans", [])
    if (
        root.get("Node Type") != "Limit"
        or len(scans) != 1
        or root.get("Total Cost", 10_001) > 10_000
    ):
        raise ValueError("PostgreSQL extraction requires a bounded LIMIT")
    scan = scans[0]
    if (
        scan.get("Node Type") not in ("Index Scan", "Index Only Scan")
        or scan.get("Relation Name") != "matches"
        or scan.get("Index Name") != "matches_pkey"
        or scan.get("Plans")
        or any("Filter" in key for key in root.keys() | scan.keys())
    ):
        raise ValueError("PostgreSQL extraction requires an unfiltered matches PK scan")


class PostgresSource:
    """One short read-only transaction per page, against a pinned frozen source."""

    def __init__(self, connect, snapshot):
        if not re.fullmatch(r"[a-zA-Z0-9._:-]{1,128}", snapshot):
            raise ValueError("A frozen source snapshot identifier is required")
        self.connect = connect
        self.source_id = f"postgres:{snapshot}"

    def page(self, after):
        with closing(self.connect()) as db:
            db.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
            return read_page(db, after)


def read_page(db, after, upper=None):
    truncated = {"player1", "player2", "tournament_name", "nbbet_slug"}
    columns = ", ".join(f"left(m.{k},513)" if k in truncated else f"m.{k}" for k in FIELDS)
    filters = []
    params = []
    for value, clause in ((after, "m.match_id > %s::uuid"), (upper, "m.match_id <= %s::uuid")):
        if value is not None:
            filters.append(clause)
            params.append(value)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    query = f"SELECT {columns} FROM public.matches m{where} ORDER BY m.match_id LIMIT %s"
    bound = (*params, PAGE_SIZE)
    with db, db.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '5s'")
        cursor.execute("SET LOCAL lock_timeout = '500ms'")
        cursor.execute("SET LOCAL idle_in_transaction_session_timeout = '10s'")
        cursor.execute("EXPLAIN (FORMAT JSON) " + query, bound)
        validate_plan(cursor.fetchone()[0])
        cursor.execute(query, bound)
        results = cursor.fetchmany(PAGE_SIZE + 1)
    if len(results) > PAGE_SIZE:
        raise ValueError("Source exceeded page size")
    return [
        {**dict(zip(FIELDS, map(iso, row), strict=True)), "source_ref": f"public.matches/{row[0]}"}
        for row in results
    ]


# Fixed existing application settings, read on the server only.
CONFIG = Path("/root/Tennis_pelmen_model/.env")
MAX_FRAME = 8 * 1024 * 1024


def connection_factory():
    import psycopg2
    from dotenv import dotenv_values

    with CONFIG.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536 or b"\x00" in raw:
        raise ValueError("Invalid server configuration")
    config = dotenv_values(stream=StringIO(raw.decode()), interpolate=False)
    keys = ("HOST", "PORT", "USER", "PASSWORD", "DB")
    settings = [config.get(f"POSTGRES_{name}") for name in keys]
    if any(not item or len(item) > 4096 or any(c in item for c in "\r\n\x00") for item in settings):
        raise ValueError("Missing server configuration")
    host, port, user, password, database = settings

    def connect():
        return psycopg2.connect(
            host=host,
            port=int(port),
            user=user,
            password=password,
            dbname=database,
            connect_timeout=5,
            application_name="player-registry-readonly",
            options="-c default_transaction_read_only=on -c search_path=pg_catalog -c timezone=UTC",
        )

    return connect, password


def highwater(connect):
    with closing(connect()) as db:
        db.set_session(readonly=True, autocommit=False)
        with db, db.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SET LOCAL lock_timeout = '500ms'")
            query = "SELECT m.match_id FROM public.matches m ORDER BY m.match_id DESC LIMIT 1"
            cursor.execute("EXPLAIN (FORMAT JSON) " + query)
            validate_plan(cursor.fetchone()[0])
            cursor.execute(query)
            row = cursor.fetchone()
            return str(row[0]) if row else None


def checked_uuid(value):
    if value is not None and (not isinstance(value, str) or str(UUID(value)) != value):
        raise ValueError("Invalid cursor boundary")
    return value


def frame(value, stream):
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) >= MAX_FRAME:
        raise ValueError("Oversize export frame")
    stream.write(raw + b"\n")
    stream.flush()


def export(request, connect, password, stream):
    after = checked_uuid(request["after"])
    upper = checked_uuid(request["upper"]) if request["pinned"] else highwater(connect)
    if after is not None and (upper is None or after > upper):
        raise ValueError("Invalid extraction bounds")
    frame({"upper": upper}, stream)
    deadline = monotonic() + MAX_SECONDS
    # A single connection serves bounded, individually committed read-only pages.
    if upper is not None:
        with closing(connect()) as db:
            db.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
            for _ in range(MAX_PAGES):
                if monotonic() >= deadline:
                    break
                rows = read_page(db, after, upper)
                if not rows:
                    frame({"done": True}, stream)
                    return
                if password and any(
                    (password in val if len(password) >= 8 else password == val)
                    for row in rows
                    for val in row.values()
                    if isinstance(val, str)
                ):
                    raise ValueError("Sensitive content in an export field")
                frame({"rows": rows}, stream)
                after = rows[-1]["match_id"]
                sleep(0.1)
        frame({"pause": True}, stream)
    else:
        frame({"done": True}, stream)


if __name__ == "__main__":
    # When streamed, the reviewed runner sets this literal before the script.
    try:
        import logging

        logging.disable(logging.CRITICAL)
        connect_db, db_password = connection_factory()
        export(globals()["REQUEST"], connect_db, db_password, sys.stdout.buffer)
    except Exception:  # noqa: BLE001 -- no database details leave the server on failure
        print("player_registry_readonly_export_failed", file=sys.stderr)
        raise SystemExit(1) from None
