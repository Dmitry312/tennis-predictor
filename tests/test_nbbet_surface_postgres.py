"""Disposable PostgreSQL integration tests for surface persistence (never production)."""
import os
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, select

from tennis_predictor.ingestion.nbbet import detail_job
from tennis_predictor.storage.models import Match


@pytest.fixture
def pg_engine():
    url = os.environ.get("NBBET_SURFACE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable PostgreSQL service")
    engine = create_engine(url)
    Match.__table__.create(engine, checkfirst=True)
    try:
        yield engine
    finally:
        Match.__table__.drop(engine, checkfirst=True)
        engine.dispose()


def _row(mid, *, surface=None):
    return {
        "match_id": mid, "nbbet_match_id": int(mid.int % 2_000_000_000),
        "gender": "M", "level": "tour", "match_date": date(2026, 11, 12),
        "surface": surface, "status": 3,
    }


def test_flush_persists_new_surface_without_overwriting_existing(monkeypatch, pg_engine):
    monkeypatch.setattr(detail_job, "engine", pg_engine)
    monkeypatch.setattr(detail_job, "write_parquet_to_s3", lambda *args: None)
    fresh, existing, unknown = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(insert(Match), [
            _row(fresh), _row(existing, surface="grass"), _row(unknown),
        ])

    def outcome(mid, surface):
        return detail_job.Outcome(
            row=SimpleNamespace(match_id=mid, gender="M", level="tour",
                                match_date=date(2026, 11, 12), nbbet_match_id=1),
            kind="ok", status=3, surface=surface,
            detail_row={"details_version": 3}, stat_rows=[], raw_row=None,
        )

    detail_job.flush([
        outcome(fresh, "hard"), outcome(existing, "clay"), outcome(unknown, None),
    ], "test", 0, None, "test-bucket", False)
    with pg_engine.connect() as conn:
        values = {mid: surface for mid, surface in conn.execute(select(Match.match_id, Match.surface))}
    assert values[fresh] == "hard"
    assert values[existing] == "grass"
    assert values[unknown] is None


def test_flush_saves_surface_on_retry_later_and_status_changed(monkeypatch, pg_engine):
    monkeypatch.setattr(detail_job, "engine", pg_engine)
    a, b = uuid.uuid4(), uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(insert(Match), [_row(a), _row(b)])
    outcomes = [
        detail_job.Outcome(row=SimpleNamespace(match_id=a), kind="retry_later", surface="clay"),
        detail_job.Outcome(row=SimpleNamespace(match_id=b), kind="status_changed",
                           status=5, surface="grass"),
    ]
    detail_job.flush(outcomes, "test", 0, None, "test-bucket", False)
    with pg_engine.connect() as conn:
        values = {mid: surface for mid, surface in conn.execute(select(Match.match_id, Match.surface))}
    assert values[a] == "clay"
    assert values[b] == "grass"
