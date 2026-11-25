import copy
import json
import os
import uuid

import pytest

from tennis_predictor.ml import player_identity as m


def eid(n):
    return str(uuid.UUID(int=n))


def provenance(t="2026-01-02T00:00:00Z"):
    return {"source": "synthetic", "reference": "synthetic-local-evidence",
            "actor": "synthetic-reviewer", "observed_at": None, "recorded_at": t}


def create(n, name="Synthetic Person", gender="M"):
    return {"op": "create", "event_id": eid(n), "name": name,
            "gender": gender, "provenance": provenance()}


def alias(n, pid, name):
    return {"op": "alias", "event_id": eid(n), "player_id": pid,
            "name": name, "provenance": provenance()}


@pytest.mark.parametrize("left,right", [
    ("  Synthetic   Person ", "synthetic person"), ("Émile", "E\u0301mile"),
    ("NAME\u00a0PERSON", "name person")])
def test_conservative_normalization(left, right):
    assert m.normalize_name(left) == m.normalize_name(right)


@pytest.mark.parametrize("left,right", [
    ("José", "Jose"), ("A. Person", "A Person"), ("Person A", "A Person"),
    ("Straße", "Strasse"), ("Алекс", "Aleks"), ("A-Person", "A Person")])
def test_no_lossy_or_fuzzy_normalization(left, right):
    assert m.normalize_name(left) != m.normalize_name(right)


@pytest.mark.parametrize("name", [None, 3, "", " ", "123", "A/B", "A\nB", "A\u200bB", "x"*201])
def test_invalid_names(name):
    with pytest.raises(m.IdentityError):
        m.normalize_name(name)


def test_ids_persist_and_are_not_name_keys():
    r = m.PlayerRegistry()
    first = r.apply(create(1))
    second = r.apply(create(2))
    assert first != second
    assert r.resolve("M", "Synthetic Person").status == "quarantine"
    assert m.PlayerRegistry.replay(r.events).digest == r.digest
    assert m.PlayerRegistry.from_json(r.to_json()).resolve("M", "Synthetic Person").candidates == tuple(sorted((first, second)))


def test_gender_scope_and_provisional_resolution():
    r = m.PlayerRegistry()
    male, female = r.apply(create(1)), r.apply(create(2, gender="F"))
    for gender, expected in [("M", male), ("F", female)]:
        result = r.resolve(gender, "synthetic person")
        assert result.player_id == expected and result.status == "provisional"
        assert result.reason == "name_only" and not result.verified
        assert result.registry_digest == r.digest
    assert r.resolve(None, "synthetic person").reason == "unknown_gender"
    assert r.resolve("M", "Different").reason == "unmatched_name"
    assert r.resolve("M", None).reason == "invalid_name"


def test_explicit_alias_and_provenance():
    r = m.PlayerRegistry()
    pid = r.apply(create(1, "J. Synthetic"))
    assert r.resolve("M", "Jean Synthetic").player_id is None
    r.apply(alias(2, pid, "Jean Synthetic"))
    assert r.resolve("M", "Jean Synthetic").player_id == pid
    assert not r.resolve("M", "Jean Synthetic").verified
    assert r.events[1]["provenance"] == provenance()


def test_collisions_quarantine_and_retractions_preserve_history():
    r = m.PlayerRegistry()
    first = r.apply(create(1))
    second = r.apply(create(2))
    old = r.resolve("M", "Synthetic Person")
    assert old.player_id is None and len(old.candidates) == 2
    r.apply({"op": "retract", "event_id": eid(3), "alias_event_id": eid(2), "provenance": provenance()})
    assert r.resolve("M", "Synthetic Person").player_id == first
    assert r.at_revision(2).resolve("M", "Synthetic Person") == old
    r.apply(alias(4, second, "Other Synthetic"))
    assert r.resolve("M", "Other Synthetic").player_id == second


def test_idempotency_and_conflicting_event_atomicity():
    r = m.PlayerRegistry()
    event = create(1)
    pid = r.apply(event)
    before = r.to_json()
    assert r.apply(copy.deepcopy(event)) == pid and r.to_json() == before
    event["name"] = "Conflicting"
    with pytest.raises(m.IdentityError, match="event_conflict"):
        r.apply(event)
    assert r.to_json() == before


@pytest.mark.parametrize("field,value", [("rank", 30), ("photo", "synthetic"),
                                       ("verified", True), ("source_player_id", "synthetic")])
def test_no_implicit_identity_evidence(field, value):
    event = create(1)
    event[field] = value
    with pytest.raises(m.IdentityError, match="invalid_event_schema"):
        m.PlayerRegistry().apply(event)


def test_state_and_input_cannot_be_mutated_through_exports():
    r = m.PlayerRegistry()
    event = create(1)
    r.apply(event)
    before = r.digest
    event["provenance"]["actor"] = "Changed"
    exported = r.events
    exported[0]["name"] = "Changed"
    assert r.digest == before
    assert "Synthetic Person" not in repr(r)


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(gender="unknown"),
    lambda e: e.update(op=[]),
    lambda e: e.update(event_id="not-uuid"),
    lambda e: e["provenance"].pop("actor"),
    lambda e: e["provenance"].update(observed_at="2027-01-01T00:00:00Z"),
    lambda e: e["provenance"].update(recorded_at="2026-01-01"),
])
def test_invalid_events_are_atomic(mutation):
    r = m.PlayerRegistry()
    r.apply(create(1))
    before = r.to_json()
    event = create(2)
    mutation(event)
    with pytest.raises(m.IdentityError):
        r.apply(event)
    assert r.to_json() == before


def test_replay_order_version_and_tamper_rejected():
    r = m.PlayerRegistry()
    r.apply(create(1))
    event = create(2)
    event["provenance"]["recorded_at"] = "2025-01-01T00:00:00Z"
    with pytest.raises(m.IdentityError, match="journal_order"):
        r.apply(event)
    d = json.loads(r.to_json())
    d["events"][0]["name"] = "Tampered"
    with pytest.raises(m.IdentityError, match="digest_mismatch"):
        m.PlayerRegistry.from_json(json.dumps(d))
    d = json.loads(r.to_json())
    d["normalization_version"] = "future"
    with pytest.raises(m.IdentityError, match="unsupported_version"):
        m.PlayerRegistry.from_json(json.dumps(d))
    with pytest.raises(m.IdentityError, match="duplicate_event"):
        m.PlayerRegistry.replay([create(1), create(1)])
    with pytest.raises(m.IdentityError):
        m.PlayerRegistry.from_json('{"schema_version":1,"schema_version":2}')


def test_local_persistence_atomic_private_and_no_overwrite(tmp_path):
    r = m.PlayerRegistry()
    pid = r.apply(create(1))
    target = tmp_path/"registry.json"
    r.save(target)
    loaded = m.PlayerRegistry.load(target)
    assert loaded.to_json() == r.to_json()
    assert loaded.resolve("M", "Synthetic Person").player_id == pid
    assert os.stat(target).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        r.save(target)
    assert list(tmp_path.iterdir()) == [target]


def test_failed_publication_removes_temporary_file(tmp_path, monkeypatch):
    r = m.PlayerRegistry()
    def fail(*args):
        raise OSError("synthetic")
    monkeypatch.setattr(m.os, "link", fail)
    with pytest.raises(OSError):
        r.save(tmp_path/"registry.json")
    assert not list(tmp_path.iterdir())


def test_event_and_snapshot_limits(monkeypatch):
    r = m.PlayerRegistry()
    monkeypatch.setattr(m, "MAX_EVENTS", 1)
    r.apply(create(1))
    before = r.digest
    with pytest.raises(m.IdentityError, match="event_limit"):
        r.apply(create(2))
    assert r.digest == before
    monkeypatch.setattr(m, "MAX_BYTES", 256)
    with pytest.raises(m.IdentityError):
        m.PlayerRegistry().apply(create(3))
    with pytest.raises(m.IdentityError):
        m.PlayerRegistry.from_json("x"*257)


def test_unknown_player_retract_and_missing_provenance():
    r = m.PlayerRegistry()
    with pytest.raises(m.IdentityError, match="unknown_player"):
        r.apply(alias(2, eid(1), "Alias"))
    with pytest.raises(m.IdentityError):
        r.apply({"op": "retract", "event_id": eid(2), "alias_event_id": eid(1), "provenance": provenance()})
    event = create(1)
    event.pop("provenance")
    with pytest.raises(m.IdentityError):
        r.apply(event)
    assert r.revision == 0
