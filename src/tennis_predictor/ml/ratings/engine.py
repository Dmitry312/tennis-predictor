"""Immutable, incremental historical rating snapshots."""

import fcntl
import json
import math
import sqlite3
import struct
import tempfile
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict
from datetime import date
from itertools import groupby
from pathlib import Path

from tennis_predictor.ml.canonical_layer.evidence import encoded, fingerprint
from tennis_predictor.ml.canonical_layer.storage import checksum

from .algorithms import MODELS, Config, State, inactive, initial, probability, surface_state, update

VERSION = "ratings-v1"
DEFAULT_CONFIG = Config()
PACK = struct.Struct("<dddI")
CODE = fingerprint(
    {
        name: checksum(Path(__file__).with_name(name))
        for name in ("algorithms.py", "inputs.py", "engine.py")
    }
)
SCHEMA = """
CREATE TABLE days(day TEXT PRIMARY KEY, input TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE states(
  pool TEXT, player TEXT, model TEXT, surface TEXT, day TEXT, pre BLOB, post BLOB,
  PRIMARY KEY(pool,player,model,surface,day)
) WITHOUT ROWID;
CREATE INDEX state_day ON states(day);
CREATE TABLE predictions(
  day TEXT, match_id TEXT, pool TEXT, level TEXT, surface TEXT,
  left_id TEXT, right_id TEXT, score INTEGER, target INTEGER, revision TEXT,
  left_pre BLOB, right_pre BLOB, probabilities BLOB, residuals BLOB,
  left_idle INTEGER, right_idle INTEGER,
  PRIMARY KEY(day,match_id)
) WITHOUT ROWID;
"""


def pack(state):
    return PACK.pack(state.rating, state.rd, state.sigma, state.count)


def unpack(blob):
    return State(*PACK.unpack(blob))


def connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


class Reader:
    def __init__(self, output, version=None):
        output = Path(output)
        self.version = version or (output / "CURRENT").read_text().strip()
        if len(self.version) != 64 or any(c not in "0123456789abcdef" for c in self.version):
            raise ValueError("Invalid rating snapshot ID")
        self.folder = output / "versions" / self.version
        self.db = self.folder / "ratings.sqlite"
        self.manifest = json.loads((self.folder / "manifest.json").read_bytes())
        payload = {k: v for k, v in self.manifest.items() if k != "sha256"}
        if fingerprint(payload) != self.version or checksum(self.db) != self.manifest["sha256"]:
            raise ValueError("Invalid rating snapshot")

    def connect(self):
        return connect(self.db)


def replay(db, periods, config):
    latest = {
        (pool, pid, model, surface): (date.fromisoformat(day), unpack(post))
        for pool, pid, model, surface, day, post in db.execute(
            "SELECT pool,player,model,surface,max(day),post FROM states "
            "GROUP BY pool,player,model,surface"
        )
    }
    for day, matches, key in periods:
        games = defaultdict(list)
        pre = {}
        predictions = []

        def prepare(slot, pre=pre, day=day):
            if slot not in pre:
                model = slot[2]
                if slot not in latest:
                    pre[slot] = initial(model, config)
                else:
                    prior, state = latest[slot]
                    gap = (day - prior).days
                    # Glicko-2 includes the active period inside its update.
                    elapsed = gap if model == "glicko" else gap - 1
                    pre[slot] = inactive(model, state, elapsed, config)
            return pre[slot]

        for match in matches:
            left_states, right_states, probabilities = [], [], []
            idle = []
            for player in (match.left, match.right):
                prior = latest.get((match.gender, player, "elo", ""))
                idle.append((day - prior[0]).days if prior else -1)
            for model in MODELS:
                if model == "surface" and match.surface is None:
                    left, right = left_states[0], right_states[0]
                else:
                    scope = match.surface if model == "surface" else ""
                    a = (match.gender, match.left, model, scope)
                    b = (match.gender, match.right, model, scope)
                    left, right = prepare(a), prepare(b)
                    if model == "surface":
                        left = surface_state(left, left_states[0], config)
                        right = surface_state(right, right_states[0], config)
                    games[a].append((right, match.score, left))
                    games[b].append((left, 1 - match.score, right))
                left_states.append(left)
                right_states.append(right)
                probabilities.append(probability(left, right))
            predictions.append(
                (
                    day.isoformat(),
                    match.match_id,
                    match.gender,
                    match.level,
                    match.surface,
                    match.left,
                    match.right,
                    match.score,
                    int(match.target),
                    match.revision,
                    b"".join(map(pack, left_states)),
                    b"".join(map(pack, right_states)),
                    struct.pack("<5d", *probabilities),
                    struct.pack("<5d", *(match.score - p for p in probabilities)),
                    *idle,
                )
            )
        states = []
        for slot, rows in sorted(games.items()):
            before = pre[slot]
            if slot[2] == "surface":
                after = State(
                    before.rating
                    + config.surface_k
                    * math.fsum(
                        score - probability(own, opponent) for opponent, score, own in rows
                    ),
                    count=before.count + len(rows),
                )
            else:
                after = update(slot[2], before, [(o, s) for o, s, _ in rows], config)
            states.append((*slot, day.isoformat(), pack(before), pack(after)))
            latest[slot] = (day, after)
        db.executemany("INSERT INTO states VALUES (?,?,?,?,?,?,?)", states)
        db.executemany(
            "INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", predictions
        )
        db.execute("INSERT INTO days VALUES (?,?)", (day.isoformat(), key))


def build(output, matches, source, config=DEFAULT_CONFIG):
    output = Path(output)
    (output / "versions").mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return build_locked(output, matches, source, config)


def build_locked(output, matches, source, config):
    ordered = sorted(matches, key=lambda m: (m.day, m.match_id))
    if len({m.match_id for m in ordered}) != len(ordered):
        raise ValueError("Duplicate rating match ID")
    periods = []
    for day, group in groupby(ordered, key=lambda m: m.day):
        batch = list(group)
        key = fingerprint([{**vars(m), "day": day.isoformat()} for m in batch])
        periods.append((day, batch, key))
    payload = {
        "algorithm": VERSION,
        "code": CODE,
        "mode": "historical_research_replay",
        "config": asdict(config),
        "source": source,
        "inputs": fingerprint([(d.isoformat(), k) for d, _, k in periods]),
        "matches": len(ordered),
        "days": len(periods),
    }
    version = fingerprint(payload)
    target = output / "versions" / version
    reused = 0
    if target.exists():
        Reader(output, version)
        reused = len(periods)
    else:
        current = Reader(output) if (output / "CURRENT").exists() else None
        with tempfile.TemporaryDirectory(dir=output) as temp:
            stage = Path(temp) / "version"
            stage.mkdir()
            path = stage / "ratings.sqlite"
            with closing(sqlite3.connect(path)) as db:
                if current and all(
                    current.manifest[k] == payload[k] for k in ("algorithm", "code", "config")
                ):
                    with closing(current.connect()) as previous:
                        old = list(previous.execute("SELECT day,input FROM days ORDER BY day"))
                        for (day, _, key), entry in zip(periods, old, strict=False):
                            if (day.isoformat(), key) != entry:
                                break
                            reused += 1
                        if reused:
                            previous.backup(db)
                            if reused < len(old):
                                boundary = old[reused][0]
                                for table in ("days", "states", "predictions"):
                                    db.execute(f"DELETE FROM {table} WHERE day>=?", (boundary,))
                if not reused:
                    db.executescript(SCHEMA)
                replay(db, periods[reused:], config)
                db.commit()
            (stage / "manifest.json").write_bytes(encoded({**payload, "sha256": checksum(path)}))
            stage.rename(target)
    with tempfile.NamedTemporaryFile(dir=output, delete=False) as pointer:
        pointer.write((version + "\n").encode())
    Path(pointer.name).replace(output / "CURRENT")
    return {
        "version": version,
        "days_reused": reused,
        "days_replayed": len(periods) - reused,
        "matches": len(ordered),
        "bytes": (target / "ratings.sqlite").stat().st_size,
    }
