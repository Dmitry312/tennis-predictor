"""Offline coverage for historical staging and the owner review boundary."""

from contextlib import closing
from pathlib import Path
from uuid import UUID

import pytest

pytest.importorskip("openpyxl")

from openpyxl import load_workbook

from tennis_predictor.ml.identity_review import staging
from tennis_predictor.ml.identity_review.registry import (
    ExactAliasPolicy,
    ReviewedMappings,
    import_review,
)
from tennis_predictor.ml.identity_review.workbook import ALIAS, GROUP, OBS, create_workbook

ROOT = Path(__file__).resolve().parents[1]
RECORDED = "2026-11-26T10:00:00Z"


class Source:
    source_id = "fixture:frozen"

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def page(self, after):
        self.calls.append(after)
        return [row for row in self.rows if after is None or row["match_id"] > after][
            : staging.PAGE_SIZE
        ]


def match(n, name="Иванов И.", opponent="Petrov A.", **extra):
    mid = str(UUID(int=n))
    defaults = {
        "match_id": mid,
        "gender": "M",
        "level": "tour",
        "match_date": "2024-03-15",
        "start_ts": "2024-03-15T10:00:00",
        "tournament_name": "=unsafe workbook context",
        "player1": name,
        "player2": opponent,
        "nbbet_match_id": n,
        "nbbet_slug": f"{n}-match",
        "source_ref": f"fixture/matches/{mid}",
    }
    return {**defaults, **extra}


def stage_packet(tmp_path, rows):
    stage, registry, packet = (
        tmp_path / name for name in ("staging.sqlite", "registry.sqlite", "review.xlsx")
    )
    result = staging.extract(stage, Source(rows))
    assert result["dataset"]
    create_workbook(stage, registry, packet)
    return stage, registry, packet


def change_packet(path, output, actions, *, sheet="Observations0001", formula=False):
    book = load_workbook(path)
    target = book[sheet]
    width = len(
        OBS if sheet.startswith("Observations") else ALIAS if sheet.startswith("Aliases") else GROUP
    )
    for row, action in actions.items():
        for i, value in enumerate(action, width + 1):
            cell = target.cell(row, i, value)
            if not formula:
                cell.data_type = "s"
    book.save(output)
    book.close()


def alias_row(path, name):
    book = load_workbook(path, read_only=True)
    try:
        return next(
            index for index, row in enumerate(book["Aliases0001"].values, 1) if row[4] == name
        )
    finally:
        book.close()


def observed(stage, n, side):
    with closing(staging.connection(stage)) as db:
        return db.execute(
            "SELECT * FROM observations WHERE match_id=? AND side=?", (str(UUID(int=n)), side)
        ).fetchone()


def test_real_catalog_excel_and_empty_registry(tmp_path, monkeypatch):
    import socket

    def network(*args, **kwargs):
        pytest.fail("offline catalog extraction touched the network")

    monkeypatch.setattr(socket, "socket", network)
    stage = tmp_path / "evidence.sqlite"
    registry = tmp_path / "registry.sqlite"
    source = staging.CatalogSource(ROOT / "docs/ml/data_catalog")
    result = staging.extract(stage, source)
    assert result["scanned"] == 20 and result["skipped"] == 0
    assert staging.extract(stage, source) == result
    packet = tmp_path / "review.xlsx"
    report = create_workbook(stage, registry, packet)
    assert report["observations"] == 40 and report["groups"] == 40 and report["parent"] == "root"
    with pytest.raises(FileExistsError):
        create_workbook(stage, registry, packet)
    book = load_workbook(packet, read_only=True)
    try:
        assert len(list(book["Observations0001"].values)) == 1
        assert len(list(book["Aliases0001"].values)) == 41
        assert len(list(book["Singletons0001"].values)) == 41
        assert "Owner questions" in book.sheetnames
    finally:
        book.close()
    assert ReviewedMappings(registry).alias_candidates("M", "Federer R.") == ()


def test_transliteration_homonyms_conflicts_and_same_day(tmp_path):
    stage, _, packet = stage_packet(
        tmp_path,
        [
            match(1),
            match(2, "Ivanov I.", start_ts="2024-03-15T15:00:00"),
            match(3, "Sidorov I.", "Sidorov I."),
            match(4, "Ivanov I.", gender="F"),
            match(5, "Sidorov I.", "Samsonov M."),
        ],
    )
    candidate = observed(stage, 1, 1)
    assert candidate["group_id"] == observed(stage, 2, 1)["group_id"]
    assert candidate["group_id"] != observed(stage, 4, 1)["group_id"]
    with closing(staging.connection(stage)) as db:
        flags = {
            row["reason"]
            for row in db.execute(
                "SELECT reason FROM flags WHERE group_id=?", (candidate["group_id"],)
            )
        }
        opposing = {
            row["reason"]
            for row in db.execute(
                "SELECT reason FROM flags WHERE group_id=?", (observed(stage, 3, 1)["group_id"],)
            )
        }
    assert flags == {
        "possible_homonyms",
        "transliteration_or_spelling_candidate",
        "conflicting_gender_context",
    }
    assert {"opposing_sides", "same_start_distinct_matches"} <= opposing
    workbook = load_workbook(packet, data_only=False)
    try:
        assert workbook["Observations0001"].cell(2, 9).value == "=unsafe workbook context"
        assert workbook["Observations0001"].cell(2, 9).data_type == "s"
    finally:
        workbook.close()


def test_review_round_trip_aliases_and_explicit_correction(tmp_path):
    stage, registry, packet = stage_packet(
        tmp_path,
        [
            match(1),
            match(2, "Ivanov I.", start_ts="2024-03-15T15:00:00"),
        ],
    )
    approval = tmp_path / "approved.xlsx"
    change_packet(
        packet,
        approval,
        {
            2: ("approve", "P0001", "Owner verifies first observation"),
            3: ("merge", "P0001", "Owner verifies cross-script alias"),
        },
    )
    first = import_review(stage, registry, approval, reviewer="owner", recorded_at=RECORDED)
    assert first["affected_mappings"] == 2 and first["revision"] == 1
    assert import_review(stage, registry, approval, reviewer="owner", recorded_at=RECORDED) == first
    independent = import_review(
        stage, tmp_path / "independent.sqlite", approval, reviewer="owner", recorded_at=RECORDED
    )
    assert independent == first

    api = ReviewedMappings(registry)
    one, two = observed(stage, 1, 1), observed(stage, 2, 1)
    first_player = api.mapping(one["match_id"], 1, observation_sha256=one["row_hash"])
    assert first_player["status"] == "verified"
    assert api.alias_candidates("M", "Ivanov I.") == (first_player["player_id"],)
    assert (
        api.mapping(two["match_id"], 1, observation_sha256=two["row_hash"])["player_id"]
        == first_player["player_id"]
    )
    assert api.mapping(one["match_id"], 1, observation_sha256="different")["status"] == "quarantine"
    assert (
        api.mapping(one["match_id"], 2, observation_sha256=observed(stage, 1, 2)["row_hash"])[
            "status"
        ]
        == "quarantine"
    )

    next_packet = tmp_path / "next.xlsx"
    create_workbook(stage, registry, next_packet)
    correction = tmp_path / "split.xlsx"
    change_packet(next_packet, correction, {3: ("split", "P0002", "Owner identifies a homonym")})
    second = import_review(stage, registry, correction, reviewer="owner", recorded_at=RECORDED)
    assert second["earliest_affected_date"] == "2024-03-15"
    changed = list(api.invalidations(2))
    assert changed[0]["previous_player_id"] == first_player["player_id"]
    assert changed[0]["player_id"] != first_player["player_id"]
    assert (
        api.mapping(two["match_id"], 1, observation_sha256=two["row_hash"], revision=1)["player_id"]
        == first_player["player_id"]
    )
    assert api.mapping(one["match_id"], 1, observation_sha256=one["row_hash"]) == first_player
    assert (
        import_review(stage, registry, correction, reviewer="owner", recorded_at=RECORDED) == second
    )
    stale = tmp_path / "stale.xlsx"
    change_packet(next_packet, stale, {3: ("reject", "", "Another unimported decision")})
    with pytest.raises(ValueError, match="Stale workbook"):
        import_review(stage, registry, stale, reviewer="owner", recorded_at=RECORDED)


def test_formula_tamper_and_opposing_sides_roll_back(tmp_path):
    stage, registry, packet = stage_packet(tmp_path, [match(1, "Same A.", "Same A.")])
    bad = tmp_path / "formula.xlsx"
    change_packet(packet, bad, {2: ("approve", "P1", "=1+1")}, formula=True)
    with pytest.raises(ValueError, match="Formula"):
        import_review(stage, registry, bad, reviewer="owner", recorded_at=RECORDED)
    conflict = tmp_path / "opposing.xlsx"
    change_packet(packet, conflict, {2: ("approve", "P1", "conflicting")}, sheet="Groups0001")
    with pytest.raises(ValueError, match="both sides"):
        import_review(stage, registry, conflict, reviewer="owner", recorded_at=RECORDED)
    tampered = tmp_path / "tampered.xlsx"
    book = load_workbook(packet)
    book["Observations0001"].cell(2, 5, "Wrong person shown")
    book.save(tampered)
    book.close()
    with pytest.raises(ValueError, match="immutable cells"):
        import_review(stage, registry, tampered, reviewer="owner", recorded_at=RECORDED)
    with closing(staging.connection(registry)) as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM people").fetchone()[0] == 0


def test_resumable_keyset_scale_and_atomic_bad_page(tmp_path):
    rows = [match(i, start_ts=None) for i in range(1, 2200)]
    source = Source(rows)
    stage = tmp_path / "staging.sqlite"
    first = staging.extract(stage, source, pages=2)
    assert first["scanned"] == 2 * staging.PAGE_SIZE and first["dataset"] is None
    assert first["checkpoint"] == rows[2 * staging.PAGE_SIZE - 1]["match_id"]
    final = staging.extract(stage, source)
    assert final["scanned"] == len(rows) and final["dataset"]
    assert len(source.calls) == 6
    assert source.calls[2] == first["checkpoint"]
    with closing(staging.connection(stage)) as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == len(rows) * 2
    wrong = Source(rows)
    wrong.source_id = "changed-frozen-source"
    with pytest.raises(ValueError, match="different source"):
        staging.extract(stage, wrong)
    broken = Source([match(1), match(2, "A" * 513)])
    broken_path = tmp_path / "failed.sqlite"
    with pytest.raises(ValueError, match="Oversize"):
        staging.extract(broken_path, broken)
    with closing(staging.connection(broken_path)) as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        assert db.execute("SELECT checkpoint FROM extraction").fetchone()[0] is None


def test_postgres_page_is_bound_and_readonly():
    plan = [
        {
            "Plan": {
                "Node Type": "Limit",
                "Total Cost": 10,
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Relation Name": "matches",
                        "Index Name": "matches_pkey",
                    },
                ],
            }
        }
    ]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=None):
            db.calls.append((query, params))

        def fetchone(self):
            return (plan,)

        def fetchmany(self, count):
            assert count == staging.PAGE_SIZE + 1
            return []

    class Db(Cursor):
        def __init__(self):
            self.calls = []

        def cursor(self):
            return Cursor()

        def close(self):
            pass

        def set_session(self, **kwargs):
            assert kwargs == {
                "readonly": True,
                "isolation_level": "REPEATABLE READ",
                "autocommit": False,
            }

    db = Db()
    source = staging.PostgresSource(lambda: db, "frozen-backup-01")
    assert source.page(str(UUID(int=1))) == []
    query, params = db.calls[-1]
    assert "WHERE m.match_id > %s::uuid ORDER BY m.match_id LIMIT %s" in query
    assert params == (str(UUID(int=1)), staging.PAGE_SIZE)
    assert "statement_timeout" in db.calls[0][0] and "EXPLAIN" in db.calls[-2][0]
    plan[0]["Plan"]["Plans"][0]["Node Type"] = "Seq Scan"
    with pytest.raises(ValueError, match="unfiltered matches PK scan"):
        source.page(None)


def test_changed_source_quarantines_old_mapping_and_invalidates_both_dates(tmp_path):
    stage, registry, packet = stage_packet(tmp_path, [match(1)])
    approved = tmp_path / "approved.xlsx"
    change_packet(
        packet,
        approved,
        {alias_row(packet, "Иванов И."): ("approve", "P0001", "Confirmed alias")},
        sheet="Aliases0001",
    )
    import_review(stage, registry, approved, reviewer="owner", recorded_at=RECORDED)
    original = observed(stage, 1, 1)
    revised_stage = tmp_path / "corrected.sqlite"
    revised = Source([match(1, match_date="2024-03-18")])
    revised.source_id = "fixture:frozen-correction"
    staging.extract(revised_stage, revised)
    unreviewed = tmp_path / "new-review.xlsx"
    create_workbook(revised_stage, registry, unreviewed)
    result = import_review(
        revised_stage, registry, unreviewed, reviewer="owner", recorded_at=RECORDED
    )
    assert result["earliest_affected_date"] == "2024-03-15"
    mapping = ReviewedMappings(registry)
    assert (
        mapping.mapping(original["match_id"], 1, observation_sha256=original["row_hash"])["status"]
        == "quarantine"
    )
    event = next(mapping.invalidations(2))
    assert event["day"] == "2024-03-18" and event["previous_day"] == "2024-03-15"
    assert event["previous_player_id"] and event["player_id"] is None


def test_split_exact_homonyms_and_partitioned_review(tmp_path, monkeypatch):
    from tennis_predictor.ml.identity_review import workbook

    monkeypatch.setattr(workbook, "SHEET_ROWS", 2)
    stage, registry, packet = stage_packet(
        tmp_path,
        [
            match(1, "Same A.", "Same A."),
            match(2, "Same A.", "Opponent B."),
        ],
    )
    reviewed = tmp_path / "split.xlsx"
    change_packet(
        packet,
        reviewed,
        {
            2: ("split", "P0001", "Owner identifies the first side"),
            3: ("split", "P0002", "Owner identifies a different person"),
        },
    )
    assert (
        import_review(stage, registry, reviewed, reviewer="owner", recorded_at=RECORDED)[
            "affected_mappings"
        ]
        == 2
    )
    mapping = ReviewedMappings(registry)
    assert len(mapping.alias_candidates("M", "Same A.")) == 2
    third = observed(stage, 2, 1)
    assert (
        mapping.mapping(third["match_id"], 1, observation_sha256=third["row_hash"])["status"]
        == "quarantine"
    )


def test_new_match_resolves_only_approved_exact_alias(tmp_path):
    stage, registry, packet = stage_packet(tmp_path, [match(1)])
    reviewed = tmp_path / "approved.xlsx"
    alias = alias_row(packet, "Иванов И.")
    change_packet(
        packet,
        reviewed,
        {alias: ("approve", "P0001", "Owner verifies NBBet alias")},
        sheet="Aliases0001",
    )
    import_review(stage, registry, reviewed, reviewer="owner", recorded_at=RECORDED)
    api = ReviewedMappings(registry)
    policy = ExactAliasPolicy("synthetic owner authorization")
    manual = api.resolve_match(match(1), source="nbbet")[0]
    assert manual["status"] == "verified" and manual["provenance"] == "manual_review"
    new = match(2, "  ИВАНОВ И. ", start_ts="2024-03-16T10:00:00")
    assert api.resolve_match(new, source="nbbet")[0]["reason"] == "alias_policy_inactive"
    automatic, opponent = api.resolve_match(new, source="nbbet", policy=policy)
    assert automatic["status"] == "rule_resolved" and automatic["player_id"] == manual["player_id"]
    assert automatic["alias_evidence_revision"] == 1
    assert opponent["reason"] == "new_alias"
    assert (
        api.resolve_match(new, source="other", policy=policy)[0]["reason"]
        == "source_or_gender_unproven"
    )
    assert (
        api.resolve_match({**new, "player1": "Ivanov I."}, source="nbbet", policy=policy)[0][
            "reason"
        ]
        == "new_alias"
    )
    assert (
        api.resolve_match(match(3), source="nbbet", policy=policy)[0]["reason"]
        == "same_start_distinct_matches"
    )
    other = {**new, "player2": "Иванов И."}
    assert all(
        r["reason"] == "opposing_sides"
        for r in api.resolve_match(other, source="nbbet", policy=policy)
    )
    assert (
        api.resolve_match({**match(1), "player1": "ИВАНОВ И."}, source="nbbet", policy=policy)[0][
            "reason"
        ]
        == "rejected_or_changed_evidence"
    )


def test_streamed_export_retries_from_committed_cursor(tmp_path):
    import ast
    import io
    import json

    from scripts.export_live_player_review import LiveSource

    first, second = ({**dict.fromkeys(staging.FIELDS), **match(n)} for n in (1, 2))
    for row in (first, second):
        row["source_ref"] = f"public.matches/{row['match_id']}"
    calls = []

    class Process:
        def __init__(self, frames):
            self.stdout = io.BytesIO(b"".join(json.dumps(f).encode() + b"\n" for f in frames))

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def start(untrusted_payload):
        request = ast.literal_eval(
            untrusted_payload.splitlines()[0].split(b"=", 1)[1].decode().strip()
        )
        calls.append(request)
        last = second["match_id"]
        if len(calls) == 1:
            return Process([{"upper": last}, {"rows": [first]}])
        return Process([{"upper": last}, {"rows": [second]}, {"done": True}])

    source = LiveSource(tmp_path, "12345", start)
    state = staging.extract(tmp_path / "fixture.sqlite", source)
    assert state["scanned"] == 2 and state["dataset"]
    assert calls == [
        {"pinned": False, "upper": None, "after": None},
        {"pinned": True, "upper": second["match_id"], "after": first["match_id"]},
    ]


def test_exact_alias_with_two_reviewed_people_is_quarantined(tmp_path):
    stage, registry, packet = stage_packet(tmp_path, [match(1), match(2)])
    reviewed = tmp_path / "ambiguous.xlsx"
    scoped = tmp_path / "alias.xlsx"
    change_packet(
        packet,
        scoped,
        {alias_row(packet, "Иванов И."): ("approve", "P0001", "Known person")},
        sheet="Aliases0001",
    )
    # Both starts coincide and therefore the workbook includes both exceptions.
    change_packet(scoped, reviewed, {4: ("split", "P0002", "Different person with same name")})
    import_review(stage, registry, reviewed, reviewer="owner", recorded_at=RECORDED)
    api = ReviewedMappings(registry)
    assert len(api.alias_candidates("M", "Иванов И.")) == 2
    assert api.resolve_match(
        match(3, start_ts="2024-03-16T10:00:00"),
        source="nbbet",
        policy=ExactAliasPolicy("fixture approval"),
    )[0]["reason"] == ("ambiguous_or_unapproved_alias")
