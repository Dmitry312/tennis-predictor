"""Append-only owner decisions and an indexed match-side mapping interface."""

import json
import re
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from itertools import zip_longest
from pathlib import Path
from uuid import uuid5

from tennis_predictor.ml.canonical_layer.evidence import encoded, fingerprint, instant
from tennis_predictor.ml.player_identity import NAMESPACE, IdentityError, normalize_name

from .staging import connection, observation_evidence, review_dir, uuid_text
from .workbook import (
    ALIAS,
    GROUP,
    OBS,
    SOURCE_SCOPE,
    aliases,
    editable_rows,
    groups,
    metadata,
    observation_fields,
    observation_rows,
    show,
    values,
    workbook_reader,
)

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS revisions (
    revision INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, packet TEXT UNIQUE NOT NULL,
    parent TEXT NOT NULL, dataset TEXT NOT NULL, reviewer TEXT NOT NULL, recorded_at TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    person_key TEXT PRIMARY KEY, player_id TEXT UNIQUE NOT NULL, gender TEXT NOT NULL,
    created_revision INTEGER NOT NULL REFERENCES revisions
);
CREATE TABLE IF NOT EXISTS events (
    revision INTEGER NOT NULL REFERENCES revisions, match_id TEXT NOT NULL, side INTEGER NOT NULL,
    row_hash TEXT NOT NULL, gender TEXT, normalized TEXT, day TEXT, previous_day TEXT,
    player_id TEXT REFERENCES people(player_id), previous_player_id TEXT REFERENCES people(player_id),
    action TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
    PRIMARY KEY(revision,match_id,side)
);
CREATE INDEX IF NOT EXISTS event_match ON events(match_id,side,revision DESC);
CREATE INDEX IF NOT EXISTS event_alias ON events(gender,normalized,revision);
CREATE INDEX IF NOT EXISTS event_player ON events(player_id,revision);
CREATE TABLE IF NOT EXISTS alias_review (
    revision INTEGER NOT NULL, match_id TEXT NOT NULL, side INTEGER NOT NULL,
    source TEXT NOT NULL, authority TEXT NOT NULL,
    PRIMARY KEY(revision,match_id,side),
    FOREIGN KEY(revision,match_id,side) REFERENCES events(revision,match_id,side)
);
CREATE TABLE IF NOT EXISTS current (
    match_id TEXT NOT NULL, side INTEGER NOT NULL, revision INTEGER NOT NULL,
    gender TEXT, normalized TEXT, row_hash TEXT NOT NULL, day TEXT,
    player_id TEXT REFERENCES people(player_id), PRIMARY KEY(match_id,side),
    FOREIGN KEY(revision,match_id,side) REFERENCES events(revision,match_id,side)
);
CREATE INDEX IF NOT EXISTS current_alias ON current(gender,normalized,player_id);
"""
TEMP_DDL = """
CREATE TEMP TABLE group_actions (
    group_id TEXT PRIMARY KEY, action TEXT, person_key TEXT, reason TEXT
);
CREATE TEMP TABLE alias_actions (
    source TEXT, gender TEXT, normalized TEXT, action TEXT, person_key TEXT, reason TEXT,
    PRIMARY KEY(source,gender,normalized)
);
CREATE TEMP TABLE side_actions (
    match_id TEXT, side INTEGER, action TEXT, person_key TEXT, reason TEXT,
    PRIMARY KEY(match_id,side)
);
"""


def open_registry(path):
    path = Path(path)
    review_dir(path.parent)
    path.touch(mode=0o600, exist_ok=True)
    db = connection(path)
    try:
        db.executescript(REGISTRY_DDL)
    except BaseException:
        db.close()
        raise
    return db


def open_review(registry, stage):
    db = open_registry(registry)
    try:
        db.execute("ATTACH DATABASE ? AS staging", (Path(stage).resolve().as_uri() + "?mode=ro",))
    except BaseException:
        db.close()
        raise
    return db


def binding(book):
    if "Coverage" not in book:
        raise ValueError("Review coverage sheet is missing")
    rows = iter(book["Coverage"].iter_rows(max_row=4))
    if values(next(rows, ())) != ("item", "value"):
        raise ValueError("Invalid coverage header")
    rows = [values(row) for row in rows]
    if (
        len(rows) != 3
        or any(len(row) != 2 for row in rows)
        or [row[0] for row in rows]
        != [
            "format",
            "dataset",
            "parent",
        ]
    ):
        raise ValueError("Invalid workbook binding")
    return tuple(row[1] for row in rows)


def decisions(expected, actual, *, admit_split):
    for facts, row in zip_longest(expected, actual):
        if facts is None or row is None or tuple(map(show, facts)) != row[:-3]:
            raise ValueError("Review rows missing, reordered or immutable cells changed")
        action, person_key, reason = (value.strip() for value in row[-3:])
        action = action.lower()
        if not action:
            if person_key or reason:
                raise ValueError("Review comments need an explicit action")
            continue
        valid = {"approve", "merge", "reject"} | ({"split"} if admit_split else set())
        if action not in valid or not reason or len(reason) > 1000:
            raise ValueError("Invalid decision action or missing evidence reason")
        if action == "reject":
            if person_key:
                raise ValueError("Rejected observations cannot have a person key")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", person_key):
            raise ValueError("Use an explicit opaque person key")
        yield facts, (action, person_key, reason)


def collect_actions(book, stage, db, parent_revision):
    packet = sha256()
    with (
        closing(connection(stage)) as source,
        closing(groups(source)) as expected,
        closing(editable_rows(book, "Groups", GROUP)) as actual,
    ):
        for facts, fields in decisions((tuple(row) for row in expected), actual, admit_split=False):
            entry = (facts[0], *fields)
            db.execute("INSERT INTO group_actions VALUES (?,?,?,?)", entry)
            packet.update(encoded(["group", *entry]))
    with (
        closing(connection(stage)) as source,
        closing(aliases(source)) as expected,
        closing(editable_rows(book, "Aliases", ALIAS)) as actual,
    ):
        for facts, fields in decisions((tuple(row) for row in expected), actual, admit_split=False):
            entry = (*facts[:3], *fields)
            db.execute("INSERT INTO alias_actions VALUES (?,?,?,?,?,?)", entry)
            packet.update(encoded(["alias", *entry]))
    with (
        closing(observation_rows(db, parent_revision=parent_revision)) as expected,
        closing(editable_rows(book, "Observations", OBS)) as actual,
    ):
        for facts, fields in decisions(
            (observation_fields(row) for row in expected), actual, admit_split=True
        ):
            entry = (facts[0], facts[1], *fields)
            db.execute("INSERT INTO side_actions VALUES (?,?,?,?,?)", entry)
            packet.update(encoded(["side", *entry]))
    return packet.hexdigest()


def person(db, row, person_key, revision):
    if row["gender"] not in ("M", "F") or row["normalized"] is None:
        raise ValueError("Invalid name or gender must remain quarantined")
    found = db.execute("SELECT * FROM people WHERE person_key=?", (person_key,)).fetchone()
    if found:
        if found["gender"] != row["gender"]:
            raise ValueError("A verified person cannot span conflicting gender contexts")
        return found["player_id"]
    # The opaque creation event depends on the approved occurrence and revision,
    # not its display name, normalized spelling or owner-facing person key.
    birth = uuid5(NAMESPACE, f"review-event/{revision}/{row['match_id']}/{row['side']}")
    player_id = str(uuid5(NAMESPACE, str(birth)))
    db.execute(
        "INSERT INTO people VALUES (?,?,?,?)", (person_key, player_id, row["gender"], revision)
    )
    return player_id


def write_events(db, revision):
    sql = f"""
        SELECT o.*, {SOURCE_SCOPE} AS source, c.player_id AS previous_player_id, c.row_hash AS previous_row_hash, c.day AS previous_day,
               coalesce(s.action,a.action,g.action) AS action,
               coalesce(s.person_key,a.person_key,g.person_key) AS person_key,
               coalesce(s.reason,a.reason,g.reason) AS reason,
               CASE WHEN s.action IS NOT NULL THEN 'exception'
                    WHEN a.action IS NOT NULL THEN 'alias'
                    WHEN g.action IS NOT NULL THEN 'group' ELSE 'exception' END AS authority
        FROM staging.observations AS o LEFT JOIN current c USING(match_id,side)
        LEFT JOIN group_actions g USING(group_id) LEFT JOIN side_actions s USING(match_id,side)
        LEFT JOIN alias_actions a ON a.source=({SOURCE_SCOPE})
          AND a.gender=o.gender AND a.normalized=o.normalized
        ORDER BY o.match_id,o.side
    """
    count = 0
    for row in db.execute(sql):
        old = row["previous_player_id"]
        action, reason = row["action"], row["reason"]
        if action is None:
            if old is None or row["previous_row_hash"] == row["row_hash"]:
                continue
            action, reason = "quarantine", "source evidence changed; renewed review required"
        new = (
            None
            if action in ("reject", "quarantine")
            else person(db, row, row["person_key"], revision)
        )
        if old is not None and new is not None and old != new and action not in ("split", "merge"):
            raise ValueError("An existing assignment requires an explicit split or merge")
        db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision,
                row["match_id"],
                row["side"],
                row["row_hash"],
                row["gender"],
                row["normalized"],
                row["day"],
                row["previous_day"],
                new,
                old,
                action,
                reason,
                row["payload"],
            ),
        )
        db.execute(
            "INSERT INTO alias_review VALUES (?,?,?,?,?)",
            (revision, row["match_id"], row["side"], row["source"], row["authority"]),
        )
        count += 1
    if not count:
        raise ValueError("Workbook has no decisions or changed evidence to publish")

    db.execute(
        """
        INSERT INTO current
        SELECT match_id,side,revision,gender,normalized,row_hash,day,player_id
        FROM events WHERE revision=?
        ON CONFLICT(match_id,side) DO UPDATE SET revision=excluded.revision, gender=excluded.gender,
        normalized=excluded.normalized,row_hash=excluded.row_hash,day=excluded.day,player_id=excluded.player_id
    """,
        (revision,),
    )
    if db.execute(
        """
        SELECT 1 FROM events e JOIN current sibling
          ON e.match_id=sibling.match_id AND e.side!=sibling.side AND e.player_id=sibling.player_id
        WHERE e.revision=? LIMIT 1
    """,
        (revision,),
    ).fetchone():
        raise ValueError("One verified person cannot occupy both sides of a match")
    count, earliest = db.execute(
        """
        SELECT count(*), min(CASE
            WHEN day IS NULL THEN previous_day WHEN previous_day IS NULL THEN day
            ELSE min(day, previous_day) END) FROM events
        WHERE revision=? AND (player_id IS NOT NULL OR previous_player_id IS NOT NULL)
    """,
        (revision,),
    ).fetchone()
    return {"affected_mappings": count, "earliest_affected_date": earliest}


def import_review(stage, registry, workbook, *, reviewer, recorded_at):
    timestamp = instant(recorded_at)
    if timestamp is None or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", reviewer):
        raise ValueError("Review requires an actor and timezone-aware decision time")
    recorded_at = timestamp.isoformat()
    with closing(open_review(registry, stage)) as db, closing(workbook_reader(workbook)) as book:
        db.executescript(TEMP_DDL)
        db.execute("BEGIN IMMEDIATE")
        current_binding, _ = metadata(db)
        book_binding = binding(book)
        if book_binding[:2] != current_binding[:2]:
            raise ValueError("Workbook belongs to a different dataset or format")
        parent = book_binding[2]
        previous = db.execute("SELECT * FROM revisions WHERE token=?", (parent,)).fetchone()
        if previous is None and parent != "root":
            raise ValueError("Unknown parent revision")
        parent_revision = previous["revision"] if previous else 0
        decisions_sha = collect_actions(book, stage, db, parent_revision)
        packet = fingerprint([book_binding, decisions_sha])
        prior = db.execute("SELECT summary FROM revisions WHERE packet=?", (packet,)).fetchone()
        if prior:
            db.commit()
            return json.loads(prior["summary"])
        if parent != current_binding[2]:
            raise ValueError("Stale workbook; regenerate against current registry")
        if previous and instant(previous["recorded_at"]) > timestamp:
            raise ValueError("Review time precedes parent")
        revision = parent_revision + 1
        token = fingerprint([packet, reviewer, recorded_at])
        db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?)",
            (
                revision,
                token,
                packet,
                parent,
                book_binding[1],
                reviewer,
                recorded_at,
                "{}",
            ),
        )
        summary = write_events(db, revision)
        result = {"revision": revision, "token": token, **summary}
        db.execute(
            "UPDATE revisions SET summary=? WHERE revision=?", (encoded(result).decode(), revision)
        )
        db.commit()
        return result


@dataclass(frozen=True)
class ExactAliasPolicy:
    """Provenance for the owner-approved exact NBBet alias rule."""

    owner_approval: str

    def __post_init__(self):
        if not isinstance(self.owner_approval, str) or not self.owner_approval.strip():
            raise ValueError("Exact alias policy requires owner approval evidence")


NBBET_EXACT_ALIAS_POLICY = ExactAliasPolicy("owner-confirmed-nbbet-name-stability-2026-11-26")


def quarantine(reason):
    return {"status": "quarantine", "player_id": None, "reason": reason}


def alias_rule(db, *, gender, normalized, revision):
    return list(
        db.execute(
            """
        SELECT e.player_id, max(e.revision) AS evidence_revision,
               max(CASE WHEN a.authority IN ('alias','group') THEN 1 ELSE 0 END) AS approved
        FROM events e JOIN alias_review a USING(revision,match_id,side)
        WHERE a.source='nbbet' AND e.gender=? AND e.normalized=? AND e.revision<=?
          AND e.revision=(SELECT max(last.revision) FROM events last
                          WHERE last.match_id=e.match_id AND last.side=e.side AND last.revision<=?)
        GROUP BY e.player_id
    """,
            (gender, normalized, revision, revision),
        )
    )


class ReviewedMappings:
    """Stable boundary for later canonical ingestion and Match Data Store integration."""

    def __init__(self, registry):
        self.path = Path(registry).resolve().as_uri() + "?mode=ro"

    def mapping(self, match_id, side, *, observation_sha256, revision=None):
        uuid_text(match_id)
        if type(side) is not int or side not in (1, 2):
            raise ValueError("Invalid match side")
        with closing(connection(self.path)) as db:
            row = db.execute(
                """
                SELECT events.player_id, events.row_hash, events.reason,
                       revisions.revision, revisions.token, revisions.recorded_at
                FROM events JOIN revisions USING(revision)
                WHERE match_id=? AND side=? AND (? IS NULL OR revision<=?)
                ORDER BY revision DESC LIMIT 1
            """,
                (match_id, side, revision, revision),
            ).fetchone()
        if row and row["player_id"] is not None and row["row_hash"] == observation_sha256:
            return {"status": "verified", **dict(row)}
        return {
            "status": "quarantine",
            "player_id": None,
            "reason": "missing_rejected_or_changed_evidence",
        }

    def alias_candidates(self, gender, name):
        try:
            normalized = normalize_name(name)
        except IdentityError:
            return ()
        with closing(connection(self.path)) as db:
            return tuple(
                row[0]
                for row in db.execute(
                    """
                SELECT DISTINCT player_id FROM current
                WHERE gender=? AND normalized=? AND player_id IS NOT NULL ORDER BY player_id
            """,
                    (gender, normalized),
                )
            )

    def resolve_match(self, row, *, source, policy=NBBET_EXACT_ALIAS_POLICY, revision=None):
        """Resolve two sides against a single pinned registry snapshot."""
        mid = uuid_text(row["match_id"])
        if policy is not None and not isinstance(policy, ExactAliasPolicy):
            raise TypeError("Expected an explicitly approved ExactAliasPolicy")
        with closing(connection(self.path)) as db:
            db.execute("BEGIN")
            if revision is None:
                pin = db.execute(
                    "SELECT * FROM revisions ORDER BY revision DESC LIMIT 1"
                ).fetchone()
            else:
                pin = db.execute("SELECT * FROM revisions WHERE revision=?", (revision,)).fetchone()
                if pin is None:
                    raise ValueError("Unknown registry revision")
            if pin is None:
                return (quarantine("empty_registry"), quarantine("empty_registry"))
            results = tuple(
                self.resolve_side(db, row, mid, side, source, policy, pin) for side in (1, 2)
            )
        if (
            results[0]["player_id"] is not None
            and results[0]["player_id"] == results[1]["player_id"]
        ):
            return (quarantine("opposing_sides"), quarantine("opposing_sides"))
        return results

    @staticmethod
    def resolve_side(db, row, mid, side, source, policy, pin):
        evidence = fingerprint(observation_evidence(row, side))
        event = db.execute(
            """
            SELECT e.player_id,e.row_hash,e.reason,e.revision,r.recorded_at
            FROM events e JOIN revisions r USING(revision)
            WHERE e.match_id=? AND e.side=? AND e.revision<=?
            ORDER BY e.revision DESC LIMIT 1
        """,
            (mid, side, pin["revision"]),
        ).fetchone()
        if event:
            if event["row_hash"] != evidence or event["player_id"] is None:
                return quarantine("rejected_or_changed_evidence")
            return {**dict(event), "status": "verified", "provenance": "manual_review"}
        if policy is None:
            return quarantine("alias_policy_inactive")
        if (
            source != "nbbet"
            or type(row.get("nbbet_match_id")) is not int
            or row["nbbet_match_id"] <= 0
            or row.get("championat_match_id") is not None
            or row.get("gender") not in ("M", "F")
        ):
            return quarantine("source_or_gender_unproven")
        try:
            normalized = normalize_name(row.get(f"player{side}"))
        except IdentityError:
            return quarantine("invalid_name")
        candidates = alias_rule(
            db, gender=row["gender"], normalized=normalized, revision=pin["revision"]
        )
        if len(candidates) != 1 or not candidates[0]["player_id"] or not candidates[0]["approved"]:
            return quarantine("ambiguous_or_unapproved_alias" if candidates else "new_alias")
        candidate = candidates[0]
        start = observation_evidence(row, side)["start_ts"]
        if (
            start
            and db.execute(
                """
            SELECT 1 FROM events e
            WHERE e.player_id=? AND e.match_id!=? AND e.revision<=?
              AND julianday(json_extract(e.evidence,'$.start_ts'))=julianday(?)
              AND e.revision=(SELECT max(last.revision) FROM events last
                  WHERE last.match_id=e.match_id AND last.side=e.side AND last.revision<=?)
            LIMIT 1
        """,
                (candidate["player_id"], mid, pin["revision"], start, pin["revision"]),
            ).fetchone()
        ):
            return quarantine("same_start_distinct_matches")
        return {
            "status": "rule_resolved",
            "provenance": "owner_approved_exact_alias",
            "player_id": candidate["player_id"],
            "registry_revision": pin["revision"],
            "registry_token": pin["token"],
            "alias_evidence_revision": candidate["evidence_revision"],
            "alias_source": source,
            "alias_gender": row["gender"],
            "normalized_alias": normalized,
            "owner_approval": policy.owner_approval,
            "observation_sha256": evidence,
        }

    def alias_invalidations(self, revision):
        """Keys used by downstream rule-resolved observations, including old aliases."""
        if type(revision) is not int or revision < 1:
            raise ValueError("Invalid review revision")
        with closing(connection(self.path)) as db:
            yield from map(
                dict,
                db.execute(
                    """
                WITH affected AS (
                    SELECT a.source, e.gender, e.normalized FROM events e
                    JOIN alias_review a USING(revision,match_id,side) WHERE e.revision=?
                    UNION
                    SELECT a.source, old.gender, old.normalized FROM events e
                    JOIN events old ON old.match_id=e.match_id AND old.side=e.side
                      AND old.revision=(SELECT max(prior.revision) FROM events prior
                        WHERE prior.match_id=e.match_id AND prior.side=e.side AND prior.revision<e.revision)
                    JOIN alias_review a ON a.revision=old.revision
                      AND a.match_id=old.match_id AND a.side=old.side
                    WHERE e.revision=?
                )
                SELECT source,gender,normalized FROM affected
                WHERE source='nbbet' AND gender IS NOT NULL AND normalized IS NOT NULL
                ORDER BY source,gender,normalized
            """,
                    (revision, revision),
                ),
            )

    def invalidations(self, revision):
        if type(revision) is not int or revision < 1:
            raise ValueError("Invalid review revision")
        with closing(connection(self.path)) as db:
            yield from map(
                dict,
                db.execute(
                    """
                SELECT match_id,side,day,previous_day,previous_player_id,player_id
                FROM events WHERE revision=? AND (player_id IS NOT NULL OR previous_player_id IS NOT NULL)
                ORDER BY match_id,side
            """,
                    (revision,),
                ),
            )
