"""SQLite index and transactional, content-addressed Parquet match revisions."""

import platform
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tennis_predictor.ml.canonical_layer.evidence import digest, fingerprint

from .contracts import SCHEMAS, VERSION, utc_time
from .engine import MatchAggregation


def engine_version():
    files = {path.name: digest(path.read_bytes()) for path in Path(__file__).parent.glob("*.py")}
    return fingerprint(
        [
            VERSION,
            files,
            platform.python_version(),
            pa.__version__,
            pa.cpp_build_info.version,
            platform.machine(),
        ]
    )


def pack(rows, schema):
    table = pa.Table.from_pylist(rows, schema=schema)
    table.validate(full=True)
    with pa.BufferOutputStream() as output:
        pq.write_table(table, output, compression="zstd")
        return output.getvalue().to_pybytes()


def unpack(payload):
    return pq.ParquetFile(pa.BufferReader(payload)).read().to_pylist()


def clock():
    return datetime.now(UTC)


class MatchDataStore:
    def __init__(self, filename):
        self.filename = Path(filename)
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        self.engine = engine_version()
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS revisions (
                    revision TEXT PRIMARY KEY, match_id TEXT NOT NULL,
                    match_sides BLOB NOT NULL, metrics BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inputs (
                    match_id TEXT NOT NULL, input_revision TEXT NOT NULL, engine TEXT NOT NULL,
                    revision TEXT NOT NULL REFERENCES revisions(revision),
                    PRIMARY KEY(match_id, input_revision, engine)
                );
                CREATE TABLE IF NOT EXISTS live (
                    sequence INTEGER PRIMARY KEY, match_id TEXT NOT NULL,
                    revision TEXT NOT NULL REFERENCES revisions(revision), processed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS live_match_time ON live(match_id, processed_at, sequence);
                CREATE TABLE IF NOT EXISTS players (
                    revision TEXT NOT NULL REFERENCES revisions(revision),
                    player_id TEXT NOT NULL, match_date TEXT NOT NULL, side INTEGER NOT NULL,
                    PRIMARY KEY(revision, side)
                );
                CREATE INDEX IF NOT EXISTS player_date ON players(player_id, match_date, revision);
            """)

    def connect(self):
        db = sqlite3.connect(self.filename, isolation_level=None, timeout=30)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def cached(self, match_id, input_revision):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT revision FROM inputs WHERE match_id=? AND input_revision=? AND engine=?",
                (match_id, input_revision, self.engine),
            ).fetchone()
            return row[0] if row else None

    def process(self, match, *, live=False, expected_previous=None):
        """Persist one match or correction. A live correction names its preceding revision."""
        revision = self.cached(match.match_id, match.input_revision)
        if revision is None:
            rows = MatchAggregation(match).run()
            files = {name: pack(rows[name], schema) for name, schema in SCHEMAS.items()}
            revision = fingerprint({name: digest(data) for name, data in files.items()})
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT revision FROM inputs WHERE match_id=? AND input_revision=? AND engine=?",
                (match.match_id, match.input_revision, self.engine),
            ).fetchone()
            if existing and existing[0] != revision:
                raise ValueError("Canonical input mapped to different match facts")
            if not existing:
                db.execute(
                    "INSERT OR IGNORE INTO revisions VALUES (?, ?, ?, ?)",
                    (revision, match.match_id, files["match_sides"], files["metrics"]),
                )
                db.execute(
                    "INSERT INTO inputs VALUES (?, ?, ?, ?)",
                    (match.match_id, match.input_revision, self.engine, revision),
                )
                for side in rows["match_sides"]:
                    if side["player_id"] and side["match_date"] is not None:
                        db.execute(
                            "INSERT OR IGNORE INTO players VALUES (?, ?, ?, ?)",
                            (
                                revision,
                                side["player_id"],
                                side["match_date"].isoformat(),
                                side["side"],
                            ),
                        )
            if live:
                self.activate(db, match.match_id, revision, expected_previous)
            db.commit()
        return revision

    @staticmethod
    def activate(db, match_id, revision, expected_previous):
        previous = db.execute(
            "SELECT revision, processed_at FROM live WHERE match_id=? ORDER BY sequence DESC LIMIT 1",
            (match_id,),
        ).fetchone()
        if previous and previous[0] == revision:
            return
        if expected_previous != (previous[0] if previous else None):
            raise ValueError("Live correction expected a different active revision")
        timestamp = utc_time(clock()).isoformat()
        if previous and timestamp < previous[1]:
            raise ValueError("Processing clock moved backwards")
        db.execute(
            "INSERT INTO live(match_id, revision, processed_at) VALUES (?, ?, ?)",
            (match_id, revision, timestamp),
        )

    def files(self, revision):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT match_sides, metrics FROM revisions WHERE revision=?", (revision,)
            ).fetchone()
        if row is None:
            raise KeyError(revision)
        files = dict(zip(SCHEMAS, row, strict=True))
        if (
            fingerprint({key: sha256(payload).hexdigest() for key, payload in files.items()})
            != revision
        ):
            raise ValueError("Corrupt match revision")
        return files

    def history(self, player_id, *, prediction_date, as_of):
        """Live history only. Select the last successfully processed revision before cutoff."""
        cutoff = utc_time(as_of)
        prediction_date = min(prediction_date, cutoff.date())
        with closing(self.connect()) as db:
            entries = db.execute(
                """
                SELECT live.revision, players.side, live.processed_at
                FROM players JOIN live USING(revision)
                WHERE players.player_id = ? AND players.match_date < ?
                  AND live.sequence = (
                    SELECT sequence FROM live AS previous
                    WHERE previous.match_id = live.match_id AND previous.processed_at < ?
                    ORDER BY previous.processed_at DESC, previous.sequence DESC LIMIT 1
                  )
                ORDER BY players.match_date, live.match_id, players.side
            """,
                (player_id, prediction_date.isoformat(), cutoff.isoformat()),
            ).fetchall()
        for revision, side, processed_at in entries:
            for row in unpack(self.files(revision)["metrics"]):
                identity_time = row["identity_recorded_at"]
                if (
                    row["side"] == side
                    and row["history_eligible"]
                    and identity_time is not None
                    and identity_time < cutoff
                ):
                    yield {
                        **row,
                        "revision": revision,
                        "processed_at": processed_at,
                        "availability_policy": "live_processed",
                    }

    def recent_feature_facts(self, player_id, prediction_date, as_of):
        """Fetch active past revisions in one indexed query, with a bounded cutoff."""
        from datetime import date

        from tennis_predictor.ml.player_features.engine import observations

        cutoff = utc_time(as_of)
        prediction_date = min(prediction_date, cutoff.date())
        with closing(self.connect()) as db:
            entries = db.execute(
                """
                SELECT p.match_date, l.match_id, l.revision, r.metrics
                FROM players p JOIN live l USING(revision) JOIN revisions r USING(revision)
                WHERE p.player_id=? AND p.match_date<? AND l.sequence=(
                    SELECT sequence FROM live previous
                    WHERE previous.match_id=l.match_id AND previous.processed_at<?
                    ORDER BY previous.processed_at DESC, previous.sequence DESC LIMIT 1
                )
                ORDER BY p.match_date DESC, l.match_id DESC
                """,
                (player_id, prediction_date.isoformat(), cutoff.isoformat()),
            )
            count = 0
            for day, mid, revision, payload in entries:
                rows = [
                    row
                    for row in unpack(payload)
                    if row["identity_recorded_at"] is not None
                    and row["identity_recorded_at"] < cutoff
                    and row["available_at"] is not None
                    and row["available_at"] < cutoff
                ]
                record = observations(rows, player_id, date.fromisoformat(day), revision, mid)
                if record is not None:
                    yield record
                    count += 1
                    if count == 15:
                        break
