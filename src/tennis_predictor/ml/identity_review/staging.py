"""Bounded, resumable extraction and disk-backed candidate indexing."""

import json
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date
from hashlib import sha256
from pathlib import Path
from time import monotonic
from uuid import UUID

from tennis_predictor.ml.canonical_layer.evidence import digest, encoded, fingerprint, instant
from tennis_predictor.ml.canonical_layer.source import CatalogAdapter, read
from tennis_predictor.ml.player_identity import IdentityError, normalize_name

PAGE_SIZE = 512
MAX_PAGES = 20_000
MAX_SECONDS = 25 * 60
VERSION = "player-review-v1"
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
# A deliberately lossy suggestion key. It never authenticates an alias.
TRANSLIT = dict(
    zip(
        "абвгдежзийклмнопрстуфхцчшщъыьэюя",
        (
            "a",
            "b",
            "v",
            "g",
            "d",
            "e",
            "zh",
            "z",
            "i",
            "i",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "r",
            "s",
            "t",
            "u",
            "f",
            "kh",
            "ts",
            "ch",
            "sh",
            "shch",
            "",
            "y",
            "",
            "e",
            "yu",
            "ya",
        ),
        strict=True,
    )
)


def candidate_name(name):
    try:
        normalized = normalize_name(name)
    except IdentityError:
        return None, None
    decomposed = unicodedata.normalize("NFKD", normalized)
    hint = "".join(TRANSLIT.get(c, c) for c in decomposed if not unicodedata.combining(c))
    return normalized, hint


def connection(path):
    db = sqlite3.connect(path, isolation_level=None, timeout=30, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA temp_store=FILE")
    return db


def review_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def iso(value):
    return (
        value.isoformat()
        if hasattr(value, "isoformat")
        else (str(value) if isinstance(value, UUID) else value)
    )


def uuid_text(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid canonical match ID")
    return value


class CatalogSource:
    def __init__(self, root):
        self.adapter = CatalogAdapter(root)
        self.entries = dict(self.adapter.entries())
        self.source_id = "catalog:" + fingerprint(self.entries)

    def page(self, after):
        rows = []
        for mid in sorted(self.entries):
            if after and mid <= after:
                continue
            manifest, folder = self.adapter.manifest(mid, self.entries[mid])
            path = folder / "matches.json"
            data, _ = read(path, manifest["files"][path.name])
            if len(data["rows"]) != 1 or data["rows"][0]["match_id"] != mid:
                raise ValueError("Invalid catalog match row")
            rows.append(
                {
                    **{key: data["rows"][0].get(key) for key in FIELDS},
                    "source_ref": f"samples/match_{mid}/matches.json#/rows/0",
                }
            )
            if len(rows) == PAGE_SIZE:
                break
        return rows


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
        truncated = {"player1", "player2", "tournament_name", "nbbet_slug"}
        columns = ", ".join(f"left(m.{k},513)" if k in truncated else f"m.{k}" for k in FIELDS)
        where = " WHERE m.match_id > %s::uuid" if after else ""
        query = f"SELECT {columns} FROM public.matches m{where} ORDER BY m.match_id LIMIT %s"
        params = (after, PAGE_SIZE) if after else (PAGE_SIZE,)
        with closing(self.connect()) as db:
            db.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
            with db, db.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                cursor.execute("SET LOCAL lock_timeout = '500ms'")
                cursor.execute("SET LOCAL idle_in_transaction_session_timeout = '10s'")
                cursor.execute("EXPLAIN (FORMAT JSON) " + query, params)
                validate_plan(cursor.fetchone()[0])
                cursor.execute(query, params)
                results = cursor.fetchmany(PAGE_SIZE + 1)
        return [
            {
                **dict(zip(FIELDS, map(iso, row), strict=True)),
                "source_ref": f"public.matches/{row[0]}",
            }
            for row in results
        ]


DDL = """
CREATE TABLE IF NOT EXISTS extraction (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), source TEXT NOT NULL,
    checkpoint TEXT, pages INTEGER NOT NULL DEFAULT 0,
    scanned INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
    dataset TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    match_id TEXT NOT NULL, side INTEGER NOT NULL CHECK(side IN (1,2)),
    gender TEXT, name TEXT, normalized TEXT, hint TEXT, day TEXT, start TEXT,
    group_id TEXT NOT NULL, payload TEXT NOT NULL, row_hash TEXT NOT NULL,
    PRIMARY KEY(match_id,side)
);
CREATE INDEX IF NOT EXISTS observation_group ON observations(group_id,match_id,side);
CREATE INDEX IF NOT EXISTS observation_hint ON observations(hint,gender);
CREATE TABLE IF NOT EXISTS flags (group_id TEXT, reason TEXT, PRIMARY KEY(group_id,reason));
"""

FLAG_SQL = """
INSERT OR IGNORE INTO flags
SELECT group_id, 'invalid_name_or_gender' FROM observations
WHERE normalized IS NULL OR gender NOT IN ('M','F') OR gender IS NULL;
INSERT OR IGNORE INTO flags
SELECT group_id, 'possible_homonyms' FROM observations GROUP BY group_id HAVING count(*) > 1;
INSERT OR IGNORE INTO flags
SELECT group_id, 'transliteration_or_spelling_candidate'
FROM observations GROUP BY group_id HAVING count(DISTINCT normalized) > 1;
INSERT OR IGNORE INTO flags
SELECT group_id, 'opposing_sides' FROM observations GROUP BY group_id, match_id HAVING count(*) > 1;
INSERT OR IGNORE INTO flags
SELECT group_id, 'same_start_distinct_matches' FROM observations WHERE start IS NOT NULL
GROUP BY group_id, start HAVING count(DISTINCT match_id) > 1;
INSERT OR IGNORE INTO flags
SELECT o.group_id, 'conflicting_gender_context' FROM observations AS o JOIN (
    SELECT hint FROM observations WHERE gender IN ('M','F')
    GROUP BY hint HAVING count(DISTINCT gender) > 1
) AS conflicts USING(hint);
"""


def observation_evidence(row, side):
    """Exact identity evidence payload shared by extraction and later mapping callers."""
    if type(side) is not int or side not in (1, 2):
        raise ValueError("Invalid identity observation side")
    return {
        **{k: iso(row.get(k)) for k in FIELDS if k not in ("player1", "player2")},
        "side": side,
        "name": row.get(f"player{side}"),
        "source_ref": f"{row['source_ref']}/player{side}",
    }


def row_observations(row):
    mid = uuid_text(row["match_id"])
    for k in ("player1", "player2", "tournament_name", "nbbet_slug"):
        value = row.get(k)
        if value is not None and (not isinstance(value, str) or len(value) > 512):
            raise ValueError("Oversize or invalid extracted text; checkpoint unchanged")
    names = (row.get("player1"), row.get("player2"))
    if row["level"] not in ("tour", "challenger") or any("/" in (n or "") for n in names):
        return []
    raw_day, raw_start = iso(row.get("match_date")), iso(row.get("start_ts"))
    day = date.fromisoformat(raw_day).isoformat() if raw_day else None
    start = instant(raw_start, naive_utc=True)
    if raw_start and start is None:
        raise ValueError("Invalid source start timestamp")
    observations = []
    for side, name in enumerate(names, 1):
        gender = row.get("gender")
        norm, hint = candidate_name(name)
        key = [gender, hint] if hint and gender in ("M", "F") else [mid, side]
        group = fingerprint([VERSION, key])
        payload = observation_evidence(row, side)
        observations.append(
            (
                mid,
                side,
                gender,
                name,
                norm,
                hint,
                day,
                start.isoformat() if start else None,
                group,
                encoded(payload).decode(),
                fingerprint(payload),
            )
        )
    return observations


def dataset_digest(db, state, *, table="observations", verify=False):
    if table not in ("observations", "staging.observations"):
        raise ValueError("Unsupported evidence table")
    checksum = sha256()
    checksum.update(
        encoded(
            [
                VERSION,
                state["source"],
                unicodedata.unidata_version,
                state["scanned"],
                state["skipped"],
            ]
        )
    )
    columns = "row_hash, payload" if verify else "row_hash"
    for row in db.execute(f"SELECT {columns} FROM {table} ORDER BY match_id,side"):
        if verify and digest(row["payload"].encode()) != row["row_hash"]:
            raise ValueError("Staged observation checksum mismatch")
        checksum.update(row["row_hash"].encode())
    return checksum.hexdigest()


def extract(path, source, *, pages=MAX_PAGES, pace=None):
    deadline = monotonic() + MAX_SECONDS
    if not 1 <= pages <= MAX_PAGES:
        raise ValueError("Invalid page budget")
    path = Path(path)
    review_dir(path.parent)
    path.touch(mode=0o600, exist_ok=True)
    with closing(connection(path)) as db:
        db.executescript(DDL)
        db.execute(
            "INSERT OR IGNORE INTO extraction(singleton,source) VALUES (1,?)", (source.source_id,)
        )
        for _ in range(pages):
            state = db.execute("SELECT * FROM extraction").fetchone()
            if state["source"] != source.source_id:
                raise ValueError("Staging belongs to a different source snapshot")
            if state["dataset"] or monotonic() >= deadline:
                break
            after = state["checkpoint"]
            page = source.page(after)
            if len(page) > PAGE_SIZE:
                raise ValueError("Source exceeded page size")
            db.execute("BEGIN IMMEDIATE")
            if dict(db.execute("SELECT * FROM extraction").fetchone()) != dict(state):
                raise ValueError("Another writer advanced the extraction")
            if not page:
                checksum = dataset_digest(db, state)
                for statement in FLAG_SQL.strip().split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("UPDATE extraction SET dataset=?", (checksum,))
                db.commit()
                break
            skipped = 0
            for row in page:
                mid = uuid_text(row["match_id"])
                if after is not None and after >= mid:
                    raise ValueError("Source returned nonmonotonic match IDs")
                observations = row_observations(row)
                skipped += not observations
                db.executemany(
                    "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)", observations
                )
                after = mid
            db.execute(
                """UPDATE extraction SET checkpoint=?, pages=pages+1,
                          scanned=scanned+?, skipped=skipped+?""",
                (after, len(page), skipped),
            )
            db.commit()
            if pace:
                pace()
        return dict(db.execute("SELECT * FROM extraction").fetchone())


def postgres_from_environment():
    """Explicit operator configuration only; never reads application .env."""
    import psycopg2

    if os.environ.get("IDENTITY_FROZEN_SOURCE") != "yes":
        raise ValueError("Operator must attest the configured source is frozen")
    dsn = os.environ["IDENTITY_PG_DSN"]
    return PostgresSource(
        lambda: psycopg2.connect(
            dsn, connect_timeout=5, application_name="tennis-identity-readonly"
        ),
        os.environ["IDENTITY_SOURCE_SNAPSHOT"],
    )


def payload(row):
    return json.loads(row["payload"])
