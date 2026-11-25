"""Pure offline canonical match/result contract. No production IO or odds features."""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from .player_identity import IdentityError, PlayerRegistry, normalize_name

CONTRACT_VERSION = "canonical-match-v1"
TARGET_VERSION = "score-derived-p1-v1"


class ContractError(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise ContractError(reason)


def utc(value, *, naive_utc=False):
    if value is None:
        return None
    require(isinstance(value, datetime), "invalid_timestamp")
    if value.tzinfo is None:
        require(naive_utc, "ambiguous_timestamp")
        value = value.replace(tzinfo=timezone.utc)
    require(value.utcoffset() is not None, "invalid_timestamp")
    return value.astimezone(timezone.utc)


def uid(value):
    try:
        require(isinstance(value, (str, uuid.UUID)), "invalid_match_id")
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ContractError("invalid_match_id") from None


def pairs(raw):
    require(type(raw) is str and len(raw.encode()) <= 65536, "invalid_json")
    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise ContractError("invalid_json") from None


@dataclass(frozen=True)
class ScoreContract:
    """Explicit reviewed mapping; never inferred from numeric JSON keys."""
    p1_games_key: str
    p2_games_key: str
    best_of: int
    decider: str
    evidence_ref: str
    source_schema_version: str
    p1_tiebreak_key: str | None = None
    p2_tiebreak_key: str | None = None

    def validate(self):
        require(type(self.best_of) is int and self.best_of in (3, 5), "unknown_format")
        require(self.decider in ("tiebreak7", "tiebreak10", "advantage", "match_tiebreak10"), "unknown_format")
        require(not (self.decider == "match_tiebreak10" and self.best_of != 3), "unknown_format")
        require(all(type(x) is str and 0 < len(x) <= 512 for x in (
            self.p1_games_key, self.p2_games_key, self.evidence_ref, self.source_schema_version)), "unknown_score_contract")
        require(self.p1_games_key != self.p2_games_key, "ambiguous_score_keys")
        require((self.p1_tiebreak_key is None) == (self.p2_tiebreak_key is None), "ambiguous_score_keys")
        if self.p1_tiebreak_key is not None:
            keys = (self.p1_games_key, self.p2_games_key, self.p1_tiebreak_key, self.p2_tiebreak_key)
            require(all(type(x) is str and 0 < len(x) <= 128 for x in keys) and len(set(keys)) == 4, "ambiguous_score_keys")


@dataclass(frozen=True)
class Source:
    reference: str
    content_sha256: str
    source: str = "nbbet_details"

    def validate(self):
        require(self.source == "nbbet_details", "unsupported_source")
        require(type(self.reference) is str and 0 < len(self.reference) <= 1024, "missing_provenance")
        require(type(self.content_sha256) is str and re.fullmatch("[a-f0-9]{64}", self.content_sha256) is not None, "missing_provenance")


@dataclass(frozen=True)
class Target:
    p1_win: int | None
    reason: str
    available_at: datetime | None
    origin: str = "score_derived"
    independently_verified: bool = field(default=False, init=False)
    version: str = TARGET_VERSION


@dataclass(frozen=True)
class ValidatedSet:
    p1: int
    p2: int
    unit: str
    tiebreak_p1: int | None = None
    tiebreak_p2: int | None = None


@dataclass(frozen=True)
class CanonicalMatch:
    match_id: str
    nbbet_match_id: int | None
    gender: str
    level: str
    event_time: datetime | None
    metadata_available_at: datetime | None
    ingested_at: datetime | None
    source: Source
    details_version: int | None
    status: int | None
    player1_id: str | None
    player2_id: str | None
    orientation: str | None
    identity_digest: str
    identity_revision: int
    identity_provisional: bool
    quarantined: bool
    reasons: tuple[str, ...]
    target: Target
    score_contract: ScoreContract | None
    validated_sets: tuple[ValidatedSet, ...] = ()
    surface: str | None = None
    tournament_name: str | None = None
    verified_training_eligible: bool = field(default=False, init=False)
    contract_version: str = CONTRACT_VERSION


def terminal_points(a, b, threshold):
    high, low = max(a, b), min(a, b)
    return (high == threshold and low <= threshold-2) or (high > threshold and high-low == 2)


def winner_from_sets(raw, contract):
    require(isinstance(contract, ScoreContract), "unknown_score_contract")
    contract.validate()
    sets = pairs(raw)
    require(type(sets) is list and 1 <= len(sets) <= contract.best_of, "incomplete_match")
    needed, wins = contract.best_of//2+1, [0, 0]
    for index, item in enumerate(sets):
        require(max(wins) < needed, "extra_sets_after_finish")
        require(type(item) is dict, "invalid_set")
        a, b = item.get(contract.p1_games_key), item.get(contract.p2_games_key)
        require(type(a) is int and type(b) is int and 0 <= a <= 99 and 0 <= b <= 99, "invalid_score_type")
        require(a != b, "incomplete_set")
        deciding = index == contract.best_of-1
        high, low = max(a, b), min(a, b)
        rule = contract.decider if deciding else "tiebreak7"
        if rule == "match_tiebreak10":
            require(terminal_points(a, b, 10), "illegal_match_tiebreak")
        elif rule == "advantage":
            require((high == 6 and low <= 4) or (high >= 7 and high-low == 2), "illegal_advantage_set")
        else:
            require((high == 6 and low <= 4) or (high == 7 and low in (5, 6)), "illegal_set")
        if contract.p1_tiebreak_key is not None:
            ta, tb = item.get(contract.p1_tiebreak_key), item.get(contract.p2_tiebreak_key)
            if ta is not None or tb is not None:
                require(type(ta) is int and type(tb) is int and 0 <= ta <= 99 and 0 <= tb <= 99, "invalid_tiebreak")
                require(rule != "advantage" and rule != "match_tiebreak10" and high == 7 and low == 6, "unexpected_tiebreak")
                threshold = 10 if rule == "tiebreak10" else 7
                require(terminal_points(ta, tb, threshold) and ((ta > tb) == (a > b)), "contradictory_tiebreak")
        wins[0 if a > b else 1] += 1
    require(max(wins) == needed and min(wins) < needed, "incomplete_match")
    return 1 if wins[0] > wins[1] else 0


def canonicalize(metadata, details, registry, *, source, score_contract=None):
    """Adapt explicit local Match/details rows against one pinned registry."""
    require(type(metadata) is dict and type(details) is dict and isinstance(registry, PlayerRegistry), "invalid_input")
    require(isinstance(source, Source), "missing_provenance")
    source.validate()
    if score_contract is not None:
        require(isinstance(score_contract, ScoreContract), "unknown_score_contract")
        score_contract.validate()
    mid = uid(metadata.get("match_id"))
    require(details.get("canonical_match_id") is not None and uid(details["canonical_match_id"]) == mid, "match_join_conflict")
    require("duplicate_of" in metadata, "unknown_duplicate_status")
    require(metadata["duplicate_of"] is None, "duplicate_excluded")
    require(metadata.get("level") in ("tour", "challenger") and metadata.get("gender") in ("M", "F"), "out_of_scope")
    name1, name2 = metadata.get("player1"), metadata.get("player2")
    try:
        n1, n2 = normalize_name(name1), normalize_name(name2)
    except IdentityError:
        raise ContractError("invalid_player_name") from None
    gender = metadata["gender"]
    r1, r2 = registry.resolve(gender, name1), registry.resolve(gender, name2)
    flags = set()
    if r1.status == "quarantine" or r2.status == "quarantine":
        flags.add("identity_quarantine")
    if r1.player_id is not None and r1.player_id == r2.player_id:
        flags.add("same_player_identity")
    status = details.get("status")
    require(status is None or (type(status) is int and 1 <= status <= 10), "invalid_status")
    if metadata.get("status") != status:
        flags.add("status_conflict")
    source_id = metadata.get("nbbet_match_id")
    if source_id is not None:
        require(type(source_id) is int and source_id > 0, "invalid_source_id")
    if details.get("nbbet_match_id") != source_id:
        flags.add("source_id_conflict")
    version = details.get("details_version")
    require(version is None or (type(version) is int and version in (1, 2, 3)), "unknown_details_version")
    orientation = None
    try:
        players = pairs(details.get("players_json"))
        require(type(players) is dict, "orientation_unknown")
        d1 = normalize_name(players["p1"]["1"])
        d2 = normalize_name(players["p2"]["1"])
        direct, reverse = (d1, d2) == (n1, n2), (d1, d2) == (n2, n1)
        require(direct != reverse and n1 != n2, "orientation_ambiguous")
        orientation = "same" if direct else "swapped"
    except (ContractError, IdentityError, KeyError, TypeError):
        flags.add("orientation_unknown")
    surface = metadata.get("surface")
    require(surface is None or surface in ("hard", "clay", "grass", "carpet"), "invalid_surface")
    tournament = metadata.get("tournament_name")
    require(tournament is None or (type(tournament) is str and len(tournament) <= 512), "invalid_tournament")
    event_time = utc(metadata.get("start_ts"), naive_utc=True)
    metadata_available = utc(metadata.get("available_at"))
    target_available = utc(details.get("available_at"))
    ingested = utc(details.get("fetched_at"), naive_utc=True)
    if event_time is not None and target_available is not None and target_available < event_time:
        flags.add("result_available_before_start")
    if ingested is not None and target_available is not None and ingested < target_available:
        flags.add("availability_after_ingestion")
    y, target_reason = None, "status_excluded"
    validated_sets = []
    if status == 3:
        if flags:
            target_reason = "record_quarantine"
        else:
            try:
                y = winner_from_sets(details.get("sets_json"), score_contract)
                if orientation == "swapped":
                    y = 1-y
                for index, item in enumerate(pairs(details["sets_json"])):
                    a, b = item[score_contract.p1_games_key], item[score_contract.p2_games_key]
                    ta = item.get(score_contract.p1_tiebreak_key) if score_contract.p1_tiebreak_key else None
                    tb = item.get(score_contract.p2_tiebreak_key) if score_contract.p2_tiebreak_key else None
                    if orientation == "swapped":
                        a, b, ta, tb = b, a, tb, ta
                    unit = "points" if (score_contract.decider == "match_tiebreak10"
                                       and index == score_contract.best_of-1) else "games"
                    validated_sets.append(ValidatedSet(a, b, unit, ta, tb))
                target_reason = "validated_score"
            except ContractError as exc:
                target_reason = str(exc)
                flags.add(target_reason)
    # All current name-only identities remain provisional, even with a valid score.
    reasons = tuple(sorted(flags | {"identity_provisional"}))
    return CanonicalMatch(mid, source_id, gender, metadata["level"], event_time,
        metadata_available, ingested, source, version, status, r1.player_id, r2.player_id,
        orientation, registry.digest, registry.revision, True, bool(flags), reasons,
        Target(y, target_reason, target_available), score_contract,
        validated_sets=tuple(validated_sets), surface=surface, tournament_name=tournament)


def _payload(record):
    d = asdict(record)
    for key in ("source", "ingested_at"):
        d.pop(key)
    return json.dumps(d, sort_keys=True, default=lambda x: x.isoformat(), separators=(",", ":"))


def canonical_batch(inputs, registry, *, as_of=None, limit=10000):
    """Each input: metadata/details/source/optional score_contract. Fixed-size local batch."""
    require(type(limit) is int and 0 < limit <= 10000, "invalid_limit")
    cutoff = utc(as_of)
    if cutoff is not None:
        from .player_identity import instant
        require(all(instant(e["provenance"]["recorded_at"]) <= cutoff for e in registry.events),
                "identity_after_cutoff")
    groups, counts = defaultdict(list), Counter()
    blocked = set()
    for index, item in enumerate(inputs):
        require(index < limit, "input_limit")
        counts["examined"] += 1
        try:
            record = canonicalize(item["metadata"], item["details"], registry,
                source=item["source"], score_contract=item.get("score_contract"))
            if cutoff is not None:
                require(record.metadata_available_at is not None, "unknown_metadata_availability")
                require(record.metadata_available_at <= cutoff, "metadata_after_cutoff")
                require(record.target.available_at is not None, "unknown_result_availability")
                require(record.target.available_at <= cutoff, "result_after_cutoff")
            groups[record.match_id].append(record)
        except (ContractError, KeyError, TypeError) as exc:
            code = str(exc) if isinstance(exc, ContractError) else "invalid_input"
            counts[code] += 1
            if code not in ("unknown_result_availability", "result_after_cutoff",
                            "unknown_metadata_availability", "metadata_after_cutoff"):
                # An unusable version must not silently reveal an older valid label.
                try:
                    blocked.add(uid(item["metadata"]["match_id"]))
                except (ContractError, KeyError, TypeError):
                    pass
    source_matches = defaultdict(set)
    for mid, group in groups.items():
        for r in group:
            if r.nbbet_match_id is not None:
                source_matches[r.nbbet_match_id].add(mid)
    collisions = {mid for ids in source_matches.values() if len(ids) > 1 for mid in ids}
    records = []
    for mid in sorted(groups):
        if mid in blocked:
            counts["invalid_correction_quarantine"] += 1
            continue
        if mid in collisions:
            counts["source_id_collision"] += 1
            continue
        candidates = groups[mid]
        if len(candidates) > 1:
            fingerprints = {_payload(r) for r in candidates}
            if any(r.ingested_at is None for r in candidates):
                if len(fingerprints) > 1:
                    counts["unordered_correction"] += 1
                    continue
            else:
                latest = max(r.ingested_at for r in candidates)
                candidates = [r for r in candidates if r.ingested_at == latest]
                if len({_payload(r) for r in candidates}) > 1:
                    counts["correction_conflict"] += 1
                    continue
            counts["deduplicated_or_superseded"] += len(groups[mid])-1
        chosen = min(candidates, key=lambda r: (r.source.content_sha256, r.source.reference))
        records.append(chosen)
        counts[chosen.target.reason] += 1
        for flag in chosen.reasons:
            counts["flag_"+flag] += 1
    return {"version": CONTRACT_VERSION, "records": tuple(records), "counts": dict(counts),
            "identity_digest": registry.digest, "identity_revision": registry.revision}


def prematch_context(record, registry, *, cutoff):
    """Strict context projection only; never scores, status, target, odds or rank."""
    cutoff = utc(cutoff)
    require(cutoff is not None and record.metadata_available_at is not None, "unknown_metadata_availability")
    require(record.metadata_available_at <= cutoff, "metadata_after_cutoff")
    require(record.event_time is not None and cutoff <= record.event_time, "invalid_prediction_cutoff")
    require(record.identity_digest == registry.digest and record.identity_revision == registry.revision, "identity_snapshot_mismatch")
    from .player_identity import instant
    require(all(instant(e["provenance"]["recorded_at"]) <= cutoff for e in registry.events), "identity_after_cutoff")
    require(record.player1_id is not None and record.player2_id is not None and
            record.player1_id != record.player2_id, "identity_quarantine")
    return {"match_id": record.match_id, "player1_id": record.player1_id,
            "player2_id": record.player2_id, "gender": record.gender, "level": record.level,
            "event_time": record.event_time, "surface": record.surface,
            "identity_digest": record.identity_digest,
            "identity_provisional": record.identity_provisional}
