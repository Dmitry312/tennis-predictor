"""Streaming Excel review packet, with authenticated-by-hash immutable inputs."""

import contextlib
import os
import re
import tempfile
import zipfile
from collections.abc import Iterable
from itertools import islice
from pathlib import Path

from .staging import VERSION, connection, dataset_digest, payload, review_dir

SHEET_ROWS = 500_000
EDIT = ("action", "person_key", "reason")
GROUP = (
    "group_id",
    "gender",
    "example_name",
    "observations",
    "aliases",
    "first_date",
    "last_date",
    "example_match",
    "flags",
    "confidence",
)
OBS = (
    "match_id",
    "side",
    "group_id",
    "observation_sha256",
    "name",
    "normalized",
    "gender",
    "match_date",
    "tournament",
    "level",
    "nbbet_match_id",
    "championat_match_id",
    "example_url",
    "source_ref",
    "source_start",
    "previous_player_id",
    "previous_person_key",
)
ALIAS = (
    "source",
    "gender",
    "normalized",
    "group_id",
    "example_name",
    "observations",
    "first_date",
    "last_date",
    "example_match",
)
# Only NBBet-only rows supply a usable source namespace. Dual-source rows lack
# proof of which source supplied the display name.
SOURCE_SCOPE = """CASE WHEN json_type(o.payload,'$.nbbet_match_id')='integer'
    AND json_extract(o.payload,'$.nbbet_match_id')>0
    AND json_extract(o.payload,'$.championat_match_id') IS NULL
    THEN 'nbbet' ELSE 'unknown' END"""

QUESTIONS = (
    "Is there an immutable source player ID? What is its documented field and namespace?",
    "Which spellings are equivalent across scripts or after source corrections?",
    "Are photos or numeric media path components linked to people or to replaceable media?",
    "What does the nested boolean mean? Are ranking values and timestamps reliable context only?",
    "Can the source reliably distinguish singles, homonyms and conflicting gender classifications?",
    "Which alias and transliteration variants should be explicitly approved by the owner?",
)


def show(value):
    if value is None:
        return ""
    # XML cannot contain C0 characters; exact values remain in staged evidence.
    return "".join(
        c if (ord(c) >= 32 and c not in "\ufffe\uffff") else f"\\u{ord(c):04x}" for c in str(value)
    )


def append(sheet, values):
    from openpyxl.cell import WriteOnlyCell

    cells = []
    for value in values:
        cell = WriteOnlyCell(sheet, value=show(value))
        cell.data_type = "s"  # Even values beginning with =, +, - or @ are literal text.
        cells.append(cell)
    sheet.append(cells)


def make_sheet(book, name, headers):
    sheet = book.create_sheet(name)
    sheet.freeze_panes = "A2"
    append(sheet, headers)
    return sheet


def groups(db):
    yield from db.execute("""
        WITH grouped AS (
            SELECT group_id, min(gender) AS gender, min(name) AS example_name,
                count(*) AS observations, count(DISTINCT normalized) AS aliases,
                min(day) AS first_date, max(day) AS last_date, min(match_id) AS example_match
            FROM observations GROUP BY group_id
        ),
        reasons AS (
            SELECT group_id, group_concat(reason, ', ') AS flags
            FROM (SELECT group_id,reason FROM flags ORDER BY group_id,reason) GROUP BY group_id
        )
        SELECT g.*, r.flags,
               CASE WHEN r.flags IS NOT NULL THEN 'review_priority'
                    WHEN observations=1 THEN 'singleton' ELSE 'name_candidate' END AS confidence
        FROM grouped g LEFT JOIN reasons r USING(group_id)
        ORDER BY CASE WHEN r.flags LIKE '%opposing_sides%' OR
                          r.flags LIKE '%same_start%' OR r.flags LIKE '%conflicting_gender%' THEN 0
                      WHEN r.flags IS NOT NULL THEN 1 ELSE 2 END, g.group_id
    """)


def aliases(db):
    yield from db.execute(f"""
        SELECT {SOURCE_SCOPE} AS source, gender, normalized, min(group_id),
               min(name), count(*), min(day), max(day), min(match_id)
        FROM observations o WHERE gender IN ('M','F') AND normalized IS NOT NULL
        GROUP BY source,gender,normalized ORDER BY source,gender,normalized
    """)


def observation_rows(db, *, parent_revision=None):
    """The registry connection attaches the read-only extraction as staging."""
    if parent_revision is None:
        joined = "LEFT JOIN current c USING(match_id,side)"
        args = ()
    else:
        joined = """
            LEFT JOIN events c ON c.match_id=o.match_id AND c.side=o.side
            AND c.revision=(SELECT max(e.revision) FROM events e
            WHERE e.match_id=o.match_id AND e.side=o.side AND e.revision<=?)
        """
        args = (parent_revision,)
    yield from db.execute(
        f"""
        SELECT o.*, c.player_id AS previous_player_id, p.person_key AS previous_person_key
        FROM staging.observations o {joined}
        LEFT JOIN people p ON p.player_id=c.player_id
        WHERE (c.row_hash IS NOT NULL AND c.row_hash!=o.row_hash)
           OR EXISTS (SELECT 1 FROM staging.flags f WHERE f.group_id=o.group_id
                      AND f.reason!='possible_homonyms')
        ORDER BY o.match_id,o.side
    """,
        args,
    )


def observation_fields(row):
    data = payload(row)
    slug = data.get("nbbet_slug") or ""
    link = (
        "https://nb-bet.com/tennis/events/" + slug
        if re.fullmatch(r"[0-9]+-[a-zA-Z0-9_-]+", slug)
        else None
    )
    return (
        row["match_id"],
        row["side"],
        row["group_id"],
        row["row_hash"],
        row["name"],
        row["normalized"],
        row["gender"],
        row["day"],
        data["tournament_name"],
        data["level"],
        data["nbbet_match_id"],
        data["championat_match_id"],
        link,
        data["source_ref"],
        row["start"],
        row["previous_player_id"],
        row["previous_person_key"],
    )


def partition(book, prefix, headers, rows: Iterable):
    from openpyxl.utils import get_column_letter

    sheet = None
    count = 0
    last_column = get_column_letter(len(headers))
    for index, row in enumerate(rows):
        if index % SHEET_ROWS == 0:
            if sheet is not None:
                sheet.auto_filter.ref = f"A1:{last_column}{count + 1}"
                sheet.close()
            sheet = make_sheet(book, f"{prefix}{1 + index // SHEET_ROWS:04d}", headers)
            count = 0
        append(sheet, row)
        count += 1
    if sheet is None:
        sheet = make_sheet(book, f"{prefix}0001", headers)
    sheet.auto_filter.ref = f"A1:{last_column}{count + 1}"
    sheet.close()
    return index + 1 if count else 0


def metadata(registry):
    state = dict(registry.execute("SELECT * FROM staging.extraction").fetchone())
    if not state["dataset"]:
        raise ValueError("Extraction is incomplete; resume before writing a review packet")
    if (
        dataset_digest(registry, state, table="staging.observations", verify=True)
        != state["dataset"]
    ):
        raise ValueError("Staged dataset checksum mismatch")
    parent = registry.execute(
        "SELECT token FROM revisions ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    return (VERSION, state["dataset"], parent[0] if parent else "root"), state


def create_workbook(staging, registry, destination):
    from openpyxl import Workbook

    from .registry import open_review

    destination = Path(destination)
    review_dir(destination.parent)
    if destination.exists():
        raise FileExistsError(destination)
    with contextlib.closing(open_review(registry, staging)) as db:
        db.execute("BEGIN")
        binding, state = metadata(db)
        group_count = db.execute(
            "SELECT count(DISTINCT group_id) FROM staging.observations"
        ).fetchone()[0]
        observation_count = db.execute("SELECT count(*) FROM staging.observations").fetchone()[0]
        book = Workbook(write_only=True)
        coverage = make_sheet(book, "Coverage", ("item", "value"))
        for item, value in zip(("format", "dataset", "parent"), binding, strict=True):
            append(coverage, (item, value))
        for item in ("source", "pages", "scanned", "skipped"):
            append(coverage, (item, state[item]))
        append(coverage, ("observations", observation_count))
        append(coverage, ("proposed_groups", group_count))
        append(coverage, ("stage", "owner review required"))
        instructions = make_sheet(book, "Instructions", ("instructions",))
        for line in (
            "Edit only action, person_key and reason on Groups, Aliases or Observations (exceptions).",
            "Approve a group with an explicit opaque person key such as P0001 and an evidence reason.",
            "Use merge with a shared key; split on individual observations with distinct keys.",
            "Reject quarantines. Leave unresolved decisions blank. Do not sort or remove rows.",
            "Exceptions override alias decisions, which override group decisions. Review only exceptions per match.",
            "Singletons, Conflicts and Sample are reference sheets. Make decisions on Groups, Aliases or Observations.",
        ):
            append(instructions, (line,))
        question = make_sheet(book, "Owner questions", ("question",))
        for line in QUESTIONS:
            append(question, (line,))
        with contextlib.closing(connection(staging)) as source:
            partition(
                book, "Groups", GROUP + EDIT, (tuple(row) + ("", "", "") for row in groups(source))
            )
            alias_count = partition(
                book,
                "Aliases",
                ALIAS + EDIT,
                (tuple(row) + ("", "", "") for row in aliases(source)),
            )
            exception_count = partition(
                book,
                "Observations",
                OBS + EDIT,
                (observation_fields(row) + ("", "", "") for row in observation_rows(db)),
            )
            partition(
                book,
                "Singletons",
                GROUP,
                (tuple(row) for row in groups(source) if row["observations"] == 1),
            )
            partition(
                book, "Conflicts", GROUP, (tuple(row) for row in groups(source) if row["flags"])
            )
            partition(
                book,
                "Sample",
                GROUP,
                (
                    tuple(row)
                    for row in islice((row for row in groups(source) if not row["flags"]), 25)
                ),
            )
        append(coverage, ("aliases", alias_count))
        append(coverage, ("observation_exceptions", exception_count))
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, suffix=".xlsx", delete=False
        ) as temp:
            path = Path(temp.name)
        try:
            book.save(path)
            os.link(path, destination)
        finally:
            path.unlink(missing_ok=True)
    return {
        "dataset": binding[1],
        "parent": binding[2],
        "observations": observation_count,
        "groups": group_count,
        "aliases": alias_count,
        "exceptions": exception_count,
        "workbook": str(destination),
    }


def workbook_reader(path):
    from openpyxl import load_workbook

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if (
            len(infos) > 512
            or sum(info.file_size for info in infos) > 8 * 1024**3
            or any(
                info.file_size > 128 * 1024**2
                for info in infos
                if info.filename == "xl/sharedStrings.xml"
            )
        ):
            raise ValueError("Review workbook exceeds safe import limits")
    return load_workbook(path, read_only=True, data_only=False, keep_links=False)


def values(row):
    if any(getattr(cell, "data_type", None) in ("f", "e") for cell in row):
        raise ValueError("Formula or error cells are not review decisions")
    return tuple(show(cell.value) for cell in row)


def editable_rows(book, prefix, headers):
    selected = sorted(name for name in book.sheetnames if name.startswith(prefix))
    if selected != [f"{prefix}{i:04d}" for i in range(1, len(selected) + 1)] or not selected:
        raise ValueError("Missing or invalid review sheets")
    for name in selected:
        rows = iter(book[name].iter_rows())
        if values(next(rows, ())) != headers + EDIT:
            raise ValueError("Invalid review sheet headers")
        for row in rows:
            cells = values(row)
            if len(cells) != len(headers + EDIT):
                raise ValueError("Invalid review row")
            yield cells
