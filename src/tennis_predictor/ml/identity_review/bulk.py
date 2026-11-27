"""Compact owner-policy snapshots in the existing SQLite registry journal."""

import re
from contextlib import closing

from tennis_predictor.ml.canonical_layer.evidence import encoded, fingerprint, instant

from .registry import open_review, person
from .workbook import SOURCE_SCOPE, metadata


def import_approved_groups(stage, registry, approval):
    """Replace the active bulk alias set with exactly the approved groups."""
    timestamp = instant(approval["recorded_at"])
    reference = approval["approval_ref"]
    if timestamp is None or not isinstance(reference, str) or not reference.strip():
        raise ValueError("Explicit owner approval and time are required")
    groups = approval["groups"]
    if (
        not isinstance(groups, dict)
        or not groups
        or any(
            not re.fullmatch(r"[a-f0-9]{64}", group)
            or not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key)
            for group, key in groups.items()
        )
    ):
        raise ValueError("Expected exact approved group IDs and stable owner keys")
    packet = fingerprint(["bulk_owner_policy", approval])
    with closing(open_review(registry, stage)) as db:
        db.execute("BEGIN IMMEDIATE")
        binding, _ = metadata(db)
        if approval["dataset"] != binding[1]:
            raise ValueError("Approval does not match the staged dataset")
        prior = db.execute("SELECT summary FROM revisions WHERE packet=?", (packet,)).fetchone()
        if prior:
            import json

            return json.loads(prior[0])
        parent = db.execute("SELECT * FROM revisions ORDER BY revision DESC LIMIT 1").fetchone()
        if approval["parent"] != binding[2]:
            raise ValueError("Approval does not match the current registry parent")
        if parent and instant(parent["recorded_at"]) > timestamp:
            raise ValueError("Approval precedes the registry parent")
        revision = (parent["revision"] if parent else 0) + 1
        token = fingerprint([packet, binding[2]])
        db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?)",
            (
                revision,
                token,
                packet,
                binding[2],
                binding[1],
                "bulk_owner_policy",
                timestamp.isoformat(),
                "{}",
            ),
        )
        db.execute("INSERT INTO bulk_batches VALUES (?,?)", (revision, reference))
        db.execute("CREATE TEMP TABLE approved_groups(group_id TEXT PRIMARY KEY, person_key TEXT)")
        db.executemany("INSERT INTO approved_groups VALUES (?,?)", sorted(groups.items()))
        for group, key in sorted(groups.items()):
            row = db.execute(
                "SELECT * FROM staging.observations WHERE group_id=? ORDER BY match_id,side LIMIT 1",
                (group,),
            ).fetchone()
            if row is None:
                raise ValueError("Approval references an unknown group")
            # Never infer a merge across different source groups from spelling.
            person(db, row, key, revision)
        if db.execute("""
            SELECT 1 FROM staging.observations o JOIN approved_groups g USING(group_id)
            JOIN people p USING(person_key) WHERE o.gender IS NOT p.gender OR o.normalized IS NULL LIMIT 1
        """).fetchone():
            raise ValueError("Invalid or conflicting gender/name evidence in approval")
        db.execute(
            f"""
            INSERT INTO bulk_aliases
            WITH candidates AS (
                SELECT source, gender, normalized,
                  CASE WHEN count(DISTINCT player_id)=1 THEN min(player_id) ELSE NULL END AS player_id
                FROM (
                  SELECT {SOURCE_SCOPE} AS source,o.gender,o.normalized,p.player_id
                  FROM staging.observations o JOIN approved_groups g USING(group_id)
                  JOIN people p USING(person_key)
                ) WHERE source='nbbet' GROUP BY source,gender,normalized
            )
            SELECT ?, c.source,c.gender,c.normalized,c.player_id,coalesce(old.evidence_revision,?)
            FROM candidates c LEFT JOIN bulk_aliases old
              ON old.source=c.source AND old.gender=c.gender AND old.normalized=c.normalized
              AND old.player_id IS c.player_id
              AND old.revision=(SELECT max(revision) FROM bulk_batches WHERE revision<?)
            """,
            (revision, revision, revision),
        )
        db.execute(
            """
            INSERT INTO bulk_match_quality
            SELECT ?,o.match_id FROM staging.observations o
            JOIN approved_groups g USING(group_id)
            JOIN (
              SELECT g2.person_key, x.start FROM staging.observations x
              JOIN approved_groups g2 USING(group_id) WHERE x.start IS NOT NULL
              GROUP BY g2.person_key,x.start HAVING count(DISTINCT x.match_id)>1
            ) overlap ON g.person_key=overlap.person_key AND o.start=overlap.start
            GROUP BY o.match_id
        """,
            (revision,),
        )
        counts = db.execute(
            """
            SELECT count(*),count(*) FILTER (WHERE player_id IS NULL)
            FROM bulk_aliases WHERE revision=?
        """,
            (revision,),
        ).fetchone()
        result = {
            "revision": revision,
            "token": token,
            "approved_groups": len(groups),
            "aliases": counts[0],
            "quarantined_aliases": counts[1],
            "dataset": binding[1],
            "approval_ref": reference,
            "recorded_at": timestamp.isoformat(),
            "parent": binding[2],
            "approval_sha256": packet,
        }
        db.execute(
            "UPDATE revisions SET summary=? WHERE revision=?", (encoded(result).decode(), revision)
        )
        db.commit()
        return result
