"""Pure per-match transformation. Reviewed assertions are pinned to bundle content."""

from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, UUID, uuid5

from tennis_predictor.ml.canonical_match import (
    ContractError,
    ScoreContract,
    pairs,
    winner_from_sets,
)
from tennis_predictor.ml.player_identity import IdentityError, PlayerRegistry, normalize_name

from .contracts import SCHEMAS, VERSION, temporal_gate
from .evidence import encoded, fingerprint, instant
from .quality import counter, point_by_point, statistics
from .source import compatible_versions

SCORE_KEYS = ("score_team1", "score_team2", "tiebreak_score_team1", "tiebreak_score_team2")


def identifier(value):
    return str(uuid5(NAMESPACE_URL, f"tennis-predictor/canonical-v1/{value}"))


def unpack(value):
    try:
        return pairs(value)
    except ContractError:
        return None


def normalized(value):
    try:
        return normalize_name(value)
    except IdentityError:
        return None


def score_contract(best_of, rule, ref):
    return ScoreContract(*SCORE_KEYS[:2], best_of, rule, ref, "catalog-sets-v1", *SCORE_KEYS[2:])


def orientation(meta, detail):
    players = unpack(detail.get("players_json"))
    try:
        names = tuple(normalized(players[f"p{side}"]["1"]) for side in (1, 2))
    except (TypeError, KeyError):
        return None
    expected = tuple(normalized(meta.get(f"player{side}")) for side in (1, 2))
    if None in expected or expected[0] == expected[1]:
        return None
    if names == expected:
        return "same"
    if names == expected[::-1]:
        return "swapped"
    return None


def price(value):
    try:
        number = Decimal(str(value))
        if number.is_finite() and 1 < number <= 500 and number == number.quantize(Decimal(".001")):
            return number
    except InvalidOperation:
        pass
    return None


class MatchTransform:
    def __init__(self, bundle, config, recorded_at):
        self.bundle = bundle
        self.match_id = bundle.match_id
        self.config = config
        self.recorded_at = recorded_at
        self.cutoff = instant(config.get("prediction_cutoff"))
        review = config.get("reviews", {}).get(self.match_id, {})
        self.review = (
            review if review.get("bundle_digest") == bundle.manifest["bundle_digest"] else {}
        )
        self.tables = {name: [] for name in SCHEMAS}
        self.by_source = {}
        for observation in bundle.observations:
            self.by_source.setdefault(observation.source, []).append(observation)
        self.metadata = self.by_source["matches"][0]
        if len(self.metadata.rows) != 1 or self.metadata.rows[0].get("match_id") != self.match_id:
            raise ValueError(f"Missing or ambiguous match metadata: {self.match_id}")
        self.meta = self.metadata.rows[0]
        self.event_time = instant(self.meta.get("start_ts"), naive_utc=True)
        self.event_date = (
            date.fromisoformat(self.meta["match_date"]) if self.meta.get("match_date") else None
        )
        self.players = [None, None]
        self.registry_version = None
        self.player_status = "quarantine"
        self.identity_recorded = []
        self.edition_id = None
        self.contract = None

    @staticmethod
    def json(value):
        return encoded(value).decode().rstrip("\n")

    @staticmethod
    def ref(observation, index):
        if index is None:
            return observation.reference
        separator = "/" if "#" in observation.reference else "#/"
        row_index = observation.row_indices[index] if observation.row_indices else index
        return f"{observation.reference}{separator}rows/{row_index}"

    def common(self, obs, index):
        row = obs.rows[index] if index is not None else None
        reference = self.ref(obs, index)
        return {
            "manifest_id": fingerprint([self.match_id, obs.version, obs.reference, obs.file_hash]),
            "event_time": self.event_time,
            "available_at": self.availability(obs, index),
            "availability_evidence_ref": self.availability_assertion(obs, index).get(
                "evidence_ref"
            ),
            "ingested_at": instant(row.get("fetched_at") or row.get("created_at"), naive_utc=True)
            if row
            else None,
            "recorded_at": self.recorded_at,
            "prediction_cutoff": self.cutoff,
            "source_version": obs.version,
            "content_sha256": fingerprint(row) if row else obs.file_hash,
            "evidence_status": "CONFIRMED" if row is not None else "UNKNOWN",
            "evidence_ref": reference,
            "transform_version": VERSION,
        }

    def availability_assertion(self, obs, index):
        return self.review.get("availability", {}).get(self.ref(obs, index), {})

    def availability(self, obs, index):
        assertion = self.availability_assertion(obs, index)
        if assertion.get("evidence_ref"):
            return instant(assertion.get("available_at"))
        return None

    def feature_time(self, kind, obs, index):
        if self.player_status == "quarantine":
            return False
        if self.player_status == "verified" and not self.identity_known_at_cutoff():
            return False
        if self.config["mode"] == "strict_as_of" and self.player_status != "verified":
            return False
        if kind == "statistics" and not self.stats_orientation_known(obs):
            return False
        available = self.availability(obs, index)
        row = obs.rows[index]
        receipt = instant(row.get("fetched_at"), naive_utc=True)
        if available is not None and (
            available > self.recorded_at or (receipt and available > receipt)
        ):
            return False
        if not self.selected_at_cutoff(obs):
            return False
        return temporal_gate(
            kind,
            mode=self.config["mode"],
            cutoff=self.cutoff,
            available_at=available,
            event_date=self.event_date,
        )[0]

    def selected_at_cutoff(self, obs):
        if sum(bool(candidate.rows) for candidate in self.by_source[obs.source]) <= 1:
            return True
        selection = self.review.get("selected_versions", {}).get(obs.source, {})
        recorded = instant(selection.get("recorded_at"))
        return bool(
            self.cutoff
            and recorded
            and recorded < self.cutoff
            and selection.get("evidence_ref")
            and selection.get("source_version") == obs.version
        )

    def identity_known_at_cutoff(self):
        return self.cutoff is not None and all(t < self.cutoff for t in self.identity_recorded)

    def stats_orientation_known(self, stats):
        compatible = [
            orientation(self.meta, row)
            for details in self.by_source["nbbet_details"]
            if self.compatible(stats, details)
            for row in details.rows
            if row.get("canonical_match_id") == self.match_id
        ]
        return bool(compatible) and set(compatible) == {"same"}

    def emit(self, table, obs, index, **values):
        row = self.common(obs, index)
        if "match_id" in SCHEMAS[table].names:
            row["match_id"] = self.match_id
        row.update(values)
        self.tables[table].append(row)

    def quality(self, obs, category, status, reason, ref=None, *, eligible=False):
        ref = ref or obs.reference
        self.emit(
            "data_quality",
            obs,
            None,
            quality_id=fingerprint([self.match_id, obs.version, ref, category, reason, VERSION]),
            category=category,
            source=obs.source,
            entity_ref=ref,
            status=status,
            reason=reason,
            evidence_status={"valid": "CONFIRMED", "conflict": "CONFLICT"}.get(status, "UNKNOWN"),
            coverage_state=obs.coverage,
            rule_version=VERSION,
            feature_eligible=eligible,
        )

    def link(self, obs, index, row):
        if row.get("canonical_match_id", row.get("match_id")) == self.match_id:
            return True
        self.quality(obs, "coverage", "conflict", "row_match_id_mismatch", self.ref(obs, index))
        return False

    def compatible(self, left, right):
        return compatible_versions(
            left, right, self.by_source, self.match_id, self.review.get("compatible_versions", [])
        )

    def compatible_set_count(self, stats):
        candidates = set()
        for obs in self.by_source["nbbet_details"]:
            for index, detail in enumerate(obs.rows):
                sets = unpack(detail.get("sets_json"))
                if (
                    self.link(obs, index, detail)
                    and isinstance(sets, list)
                    and self.compatible(stats, obs)
                ):
                    candidates.add(len(sets))
        return next(iter(candidates)) if len(candidates) == 1 else None

    def compare_pbp(self, points, sets, count):
        compared = False
        for details in self.by_source["nbbet_details"]:
            if not self.compatible(points, details):
                continue
            for i, row in enumerate(details.rows):
                scores = unpack(row.get("sets_json"))
                if not self.link(details, i, row) or not isinstance(scores, list):
                    continue
                expected = {
                    index: item[SCORE_KEYS[0]] + item[SCORE_KEYS[1]]
                    for index, item in enumerate(scores, 1)
                    if isinstance(item, dict)
                    and all(type(item.get(key)) is int for key in SCORE_KEYS[:2])
                }
                if len(expected) != len(scores) or self.contract is None:
                    continue
                if (
                    self.contract.decider == "match_tiebreak10"
                    and len(scores) == self.contract.best_of
                ):
                    expected[len(scores)] = 1
                actual = {index: len(games) for index, games in sets.items()}
                if expected == actual:
                    status, reason = "valid", "observed_game_counts_agree"
                elif any(n > expected.get(index, 0) for index, n in actual.items()):
                    status, reason = "conflict", "observed_games_exceed_result"
                else:
                    status, reason = "partial", "observed_games_below_result"
                self.quality(points, "pbp_result_reconciliation", status, reason, details.reference)
                compared = True
        if not compared:
            self.quality(
                points, "pbp_result_reconciliation", "unverified", "compatible_result_unproven"
            )
        compared = False
        for stats in self.by_source["nbbet_stats"]:
            if not self.compatible(points, stats):
                continue
            totals = [
                row
                for i, row in enumerate(stats.rows)
                if self.link(stats, i, row) and counter(row.get("scope")) == 0
            ]
            if len(totals) != 1:
                continue
            for side in (1, 2):
                counts = [
                    counter(totals[0].get(f"{metric}_p{side}"))
                    for metric in ("first_serve_points_all", "second_serve_points_all")
                ]
                if any(n is None for n in counts):
                    continue
                observed = count[side]
                status = "conflict" if observed > sum(counts) else "partial"
                self.quality(
                    points,
                    "pbp_statistics_reconciliation",
                    status,
                    "observed_points_exceed_service_total"
                    if status == "conflict"
                    else "full_service_point_coverage_unproven",
                    f"{stats.reference}/p{side}",
                )
                compared = True
        if not compared:
            self.quality(
                points,
                "pbp_statistics_reconciliation",
                "unverified",
                "compatible_statistics_unproven",
            )

    def identities(self):
        registry = PlayerRegistry()
        observations = []
        for side in (1, 2):
            name = self.meta.get(f"player{side}")
            gender = self.meta.get("gender")
            norm = normalized(name)
            approved = self.review.get("players", {}).get(str(side), {})
            if norm is None or gender not in ("M", "F") or "/" in name:
                continue
            identity_at = instant(approved.get("recorded_at"))
            reviewed = bool(
                approved.get("identity_key")
                and approved.get("evidence_ref")
                and identity_at
                and identity_at <= self.recorded_at
            )
            if reviewed:
                self.identity_recorded.append(identity_at)
            key = (
                f"reviewed/{gender}/{UUID(approved['identity_key'])}"
                if reviewed
                else f"{self.match_id}/{side}"
            )
            event_id = identifier(f"identity/{key}")
            reference = (
                approved["evidence_ref"]
                if reviewed
                else f"{self.ref(self.metadata, 0)}/player{side}"
            )
            pid = (
                registry.apply(
                    {
                        "op": "create",
                        "event_id": event_id,
                        "name": name,
                        "gender": gender,
                        "provenance": {
                            "source": "review" if reviewed else "catalog",
                            "reference": reference,
                            "actor": "canonical-v1",
                            "observed_at": None,
                            "recorded_at": self.recorded_at.isoformat(),
                        },
                    }
                )
                if event_id not in {item[1] for item in observations}
                else observations[0][2]
            )
            observations.append((side, event_id, pid, name, norm, reviewed, reference))
        emitted_players = set()
        registry_version = registry.digest
        self.registry_version = registry_version
        for side, _, pid, name, norm, reviewed, reference in observations:
            resolution = registry.resolve(self.meta["gender"], name)
            status = (
                "quarantine"
                if resolution.status == "quarantine"
                else ("verified" if reviewed else "provisional")
            )
            if len(observations) == 2 and observations[0][2] == observations[1][2]:
                status = "quarantine"
            if status != "quarantine":
                self.players[side - 1] = pid
            if pid not in emitted_players:
                self.emit(
                    "players",
                    self.metadata,
                    0,
                    player_id=pid,
                    gender=self.meta["gender"],
                    display_name=name,
                    resolution_status=status,
                    registry_version=registry_version,
                    evidence_ref=reference,
                )
                emitted_players.add(pid)
            self.emit(
                "player_aliases",
                self.metadata,
                0,
                alias_id=identifier(f"alias/{self.match_id}/{side}"),
                player_id=pid,
                player_side=side,
                source_name=name,
                normalized_name=norm,
                registry_version=registry_version,
                evidence_ref=reference,
            )
        if all(self.players):
            self.player_status = (
                "verified" if all(item[5] for item in observations) else "provisional"
            )
        self.quality(
            self.metadata,
            "identity",
            "valid"
            if self.player_status == "verified"
            else ("conflict" if self.player_status == "quarantine" else "unverified"),
            {
                "verified": "reviewed_identity",
                "provisional": "match_local_name_only",
                "quarantine": "missing_or_ambiguous_identity",
            }[self.player_status],
        )

    def context(self):
        m = self.meta
        context = self.review.get("tournament", {})
        if context.get("evidence_ref"):
            self.edition_id = str(UUID(context["edition_id"]))
            self.contract = score_contract(
                context["best_of"], context["decider_rule"], context["evidence_ref"]
            )
            self.contract.validate()
            self.emit(
                "tournaments",
                self.metadata,
                0,
                tournament_id=str(UUID(context["tournament_id"])),
                edition_id=self.edition_id,
                level=m.get("level"),
                name_raw=m.get("tournament_name"),
                best_of=self.contract.best_of,
                decider_rule=self.contract.decider,
                evidence_ref=context["evidence_ref"],
            )
        self.emit(
            "matches",
            self.metadata,
            0,
            source_match_id=m.get("nbbet_match_id"),
            player1_id=self.players[0],
            player2_id=self.players[1],
            player1_raw=m.get("player1"),
            player2_raw=m.get("player2"),
            edition_id=self.edition_id,
            start_time=self.event_time,
            match_date=self.event_date,
            gender=m.get("gender"),
            level=m.get("level"),
            surface=m.get("surface"),
            tournament_raw=m.get("tournament_name"),
            duplicate_of=m.get("duplicate_of"),
            status=m.get("status"),
        )
        self.quality(
            self.metadata,
            "tournament",
            "valid" if self.contract else "unverified",
            "reviewed_edition_rules" if self.contract else "edition_and_rules_unknown",
        )
        surface = m.get("surface")
        self.quality(
            self.metadata,
            "surface",
            "valid"
            if surface in ("hard", "clay", "grass", "carpet")
            else ("missing" if surface is None else "unverified"),
            "surface_observation" if surface else "surface_missing",
            eligible=surface in ("hard", "clay", "grass", "carpet")
            and self.feature_time("context", self.metadata, 0),
        )
        p1, p2 = price(m.get("odds_open_p1")), price(m.get("odds_open_p2"))
        if m.get("odds_open_p1") is not None or m.get("odds_open_p2") is not None:
            self.emit(
                "match_odds",
                self.metadata,
                0,
                price_p1=p1 if p2 else None,
                price_p2=p2 if p1 else None,
                source_status=m.get("odds_open_status"),
                odds_source=m.get("odds_open_source"),
                captured_at=instant(m.get("odds_open_captured_at"), naive_utc=True),
                model_feature_eligible=False,
            )
            self.quality(
                self.metadata,
                "odds",
                "valid" if p1 and p2 else "conflict",
                "backtest_only" if p1 and p2 else "invalid_price_pair",
            )
        self.quality(
            self.metadata, "extra_context", "unverified", "no_approved_extra_context_fields"
        )

    def corroboration(self, obs, detail, score, status):
        """Compare mapped raw evidence only with an explicit version link."""
        compatible_raw = False
        for raw in self.by_source["nbbet_raw"]:
            if not self.compatible(obs, raw):
                continue
            for index, row in enumerate(raw.rows):
                if not self.link(raw, index, row):
                    continue
                payload = unpack(row.get("raw_json"))
                payload = payload.get("match") if isinstance(payload, dict) else None
                if not isinstance(payload, dict):
                    continue
                compatible_raw = True
                players = unpack(detail.get("players_json"))
                players = players if isinstance(players, dict) else {}
                comparable = (
                    ("23", status),
                    ("24", score),
                    ("3", players.get("p1")),
                    ("5", players.get("p2")),
                )
                if row.get("nbbet_match_id") != detail.get("nbbet_match_id") or any(
                    value is not None and key in payload and payload[key] != value
                    for key, value in comparable
                ):
                    return "conflict", "compatible_raw_details_disagree"
        if compatible_raw:
            return "valid", "same_family_raw_consistent"
        return "unverified", "compatible_raw_snapshot_unproven"

    def results(self):
        version_rows = {}
        for obs in self.by_source["nbbet_details"]:
            linked = [(i, row) for i, row in enumerate(obs.rows) if self.link(obs, i, row)]
            version_rows[obs.version] = {fingerprint(row): (i, row) for i, row in reversed(linked)}
        has_details = False
        for obs in self.by_source["nbbet_details"]:
            rows = version_rows[obs.version].values()
            for index, row in sorted(rows):
                has_details = True
                ref = self.ref(obs, index)
                status = counter(row.get("status"), 1)
                scores = unpack(row.get("sets_json"))
                order = orientation(self.meta, row)
                raw_status, raw_reason = self.corroboration(obs, row, scores, status)
                self.quality(obs, "result_reconciliation", raw_status, raw_reason, ref)
                contract = self.contract or score_contract(3, "tiebreak7", "research-hypothesis")
                source_candidate = candidate = None
                score_reason = None
                if status == 3:
                    try:
                        y = winner_from_sets(row.get("sets_json"), contract)
                        source_candidate = 1 if y else 2
                        if order is not None:
                            candidate = (
                                source_candidate if order == "same" else 3 - source_candidate
                            )
                    except ContractError as exc:
                        score_reason = str(exc)
                compatible_conflict = any(
                    (r.get("sets_json"), r.get("status"), orientation(self.meta, r))
                    != (row.get("sets_json"), row.get("status"), order)
                    for other in self.by_source["nbbet_details"]
                    if other.version != obs.version and self.compatible(obs, other)
                    for _, r in version_rows[other.version].values()
                )
                available = self.availability(obs, index)
                received = instant(row.get("fetched_at"), naive_utc=True)
                approved = self.review.get("results", {}).get(ref, {})
                if (
                    len(version_rows[obs.version]) != 1
                    or raw_status == "conflict"
                    or compatible_conflict
                    or self.meta.get("nbbet_match_id") != row.get("nbbet_match_id")
                    or (self.compatible(obs, self.metadata) and status != self.meta.get("status"))
                    or (
                        available is not None
                        and (
                            (self.event_time is not None and available <= self.event_time)
                            or available > self.recorded_at
                            or (received is not None and available > received)
                        )
                    )
                ):
                    quality, reason = "conflict", "conflicting_result_evidence"
                elif status != 3:
                    quality, reason = "unverified", "non_completed_match_status"
                elif order is None:
                    quality, reason = "unverified", "orientation_unproven"
                elif score_reason:
                    quality, reason = ("conflict" if self.contract else "unverified"), score_reason
                elif self.contract is None:
                    quality, reason = "unverified", "format_unproven"
                elif not self.compatible(obs, self.metadata) or self.meta.get("status") != 3:
                    quality, reason = "unverified", "compatible_finished_match_metadata_unproven"
                elif self.meta.get("duplicate_of") is not None or self.meta.get("level") not in (
                    "tour",
                    "challenger",
                ):
                    quality, reason = "unverified", "duplicate_or_out_of_scope"
                elif self.player_status != "verified":
                    quality, reason = "unverified", "identity_unverified"
                elif not approved.get("evidence_ref") or approved.get(
                    "independent_winner_side"
                ) not in (1, 2):
                    quality, reason = "unverified", "independent_result_unproven"
                elif approved["independent_winner_side"] != candidate:
                    quality, reason = "conflict", "independent_result_disagrees"
                elif available is None:
                    quality, reason = "unverified", "availability_unknown"
                else:
                    quality, reason = "valid", "confirmed_completed_result"
                self.quality(obs, "result", quality, reason, ref)
                state_eligible = quality == "valid" and self.feature_time("result", obs, index)
                self.quality(
                    obs,
                    "player_state",
                    "valid" if state_eligible else "unverified",
                    "verified_result_before_cutoff"
                    if state_eligible
                    else "result_identity_or_time_gate",
                    ref,
                    eligible=state_eligible,
                )
                model_target = (
                    quality == "valid"
                    and self.config["mode"] == "strict_as_of"
                    and self.identity_known_at_cutoff()
                    and self.selected_at_cutoff(obs)
                    and self.event_time is not None
                    and self.cutoff <= self.event_time
                )
                self.quality(
                    obs,
                    "model_baseline_label",
                    "valid" if model_target else "unverified",
                    "label_eligible_at_prematch_cutoff"
                    if model_target
                    else "target_or_prematch_cutoff_gate",
                    ref,
                    eligible=model_target,
                )
                sets = None
                if isinstance(scores, list) and all(isinstance(item, dict) for item in scores):
                    sets = [
                        {
                            "games_p1": counter(item.get(SCORE_KEYS[0])),
                            "games_p2": counter(item.get(SCORE_KEYS[1])),
                            "duration_raw": str(item["duration"])
                            if item.get("duration") is not None
                            else None,
                            "tiebreak_p1": counter(item.get(SCORE_KEYS[2])),
                            "tiebreak_p2": counter(item.get(SCORE_KEYS[3])),
                        }
                        for item in scores
                    ]
                self.emit(
                    "match_results",
                    obs,
                    index,
                    status=status,
                    sets=sets,
                    sets_raw=row.get("sets_json"),
                    orientation=order,
                    best_of=self.contract.best_of if self.contract else None,
                    decider_rule=self.contract.decider if self.contract else None,
                    candidate_rule=("reviewed_edition" if self.contract else "hypothesis_bo3_tb7")
                    if source_candidate is not None
                    else None,
                    source_candidate_winner_side=source_candidate,
                    candidate_winner_side=candidate,
                    winner_side=candidate if quality == "valid" else None,
                    target_p1_win=int(candidate == 1) if quality == "valid" else None,
                    exclusion_reason=None if quality == "valid" else reason,
                    player_state_eligible=state_eligible,
                )
                self.details_aliases(obs, index, row, order)
                self.rankings(obs, index, row, order)
        if not has_details:
            self.emit(
                "match_results",
                self.metadata,
                0,
                status=self.meta.get("status"),
                exclusion_reason="score_observation_missing",
                player_state_eligible=False,
            )
            for category in ("result", "player_state", "model_baseline_label"):
                self.quality(self.metadata, category, "missing", "score_observation_missing")
            self.quality(self.metadata, "rankings", "missing", "no_ranking_observations")

    def details_aliases(self, obs, index, row, order):
        if order not in ("same", "swapped"):
            return
        source_players = unpack(row.get("players_json"))
        for side in (1, 2):
            match_side = side if order == "same" else 3 - side
            player = self.players[match_side - 1]
            if player is None:
                continue
            name = source_players[f"p{side}"]["1"]
            ref = f"{self.ref(obs, index)}/players_json/p{side}/1"
            self.emit(
                "player_aliases",
                obs,
                index,
                alias_id=identifier(f"alias/{self.match_id}/{obs.version}/{ref}"),
                player_id=player,
                player_side=match_side,
                source_name=name,
                normalized_name=normalized(name),
                registry_version=self.registry_version,
                evidence_ref=ref,
            )

    def rankings(self, obs, index, row, order):
        for side in (1, 2):
            rank = counter(row.get(f"p{side}_rank"), 1)
            tour = row.get(f"p{side}_rank_tour")
            mapped = side if order == "same" else (3 - side if order == "swapped" else None)
            ref = f"{self.ref(obs, index)}/p{side}"
            assertion = self.review.get("rankings", {}).get(ref, {})
            effective = (
                instant(assertion.get("effective_at")) if assertion.get("evidence_ref") else None
            )
            eligible, reason = temporal_gate(
                "ranking",
                mode=self.config["mode"],
                cutoff=self.cutoff,
                effective_at=effective,
                available_at=self.availability(obs, index),
            )
            if rank is not None:
                self.emit(
                    "player_rankings",
                    obs,
                    index,
                    player_id=self.players[mapped - 1] if mapped else None,
                    rank=rank,
                    player_side=side,
                    tour=tour,
                    effective_at=effective,
                )
            valid = (
                eligible
                and self.feature_time("context", obs, index)
                and mapped
                and self.player_status == "verified"
                and rank
                and tour in ("ATP", "WTA")
            )
            self.quality(
                obs,
                "rankings",
                "valid" if valid else ("unverified" if rank else "missing"),
                (
                    "ranking_available"
                    if valid
                    else (reason if not eligible else "ranking_identity_or_tour_unverified")
                )
                if rank
                else "observed_rank_missing",
                ref,
                eligible=bool(valid),
            )

    def run(self):
        for obs in self.bundle.observations:
            meta = obs.metadata
            self.emit(
                "data_manifest",
                obs,
                None,
                source=obs.source,
                source_ref=obs.reference,
                object_key=meta.get("object_key"),
                object_version=meta.get("version_id"),
                object_sha256=meta.get("content_sha256"),
                etag=meta.get("etag"),
                object_last_modified=instant(meta.get("last_modified")),
                ingested_at=instant(meta.get("row_receipt")),
                schema_sha256=fingerprint(meta.get("schema")) if meta.get("schema") else None,
                selection_plan=self.bundle.manifest.get("selection", {}).get("plan_sha256"),
                operation_ref=self.bundle.manifest.get("operation_ref"),
                coverage_state=obs.coverage,
                input_rows=len(obs.rows),
            )
            complete = obs.coverage in ("exact_primary_key", "complete", "exact_match_rows")
            partial = obs.rows or obs.coverage in ("decode_limit", "partial_row_groups")
            self.quality(
                obs,
                "coverage",
                "valid" if complete else ("partial" if partial else "missing"),
                obs.coverage,
            )
            # Do not equate acquisition or receipt time with availability.
            if not obs.rows:
                self.quality(obs, "availability", "missing", "no_source_rows")
            for index, row in enumerate(obs.rows):
                if obs.source == "nbbet_raw":
                    self.link(obs, index, row)
                available = self.availability(obs, index)
                self.quality(
                    obs,
                    "availability",
                    "valid" if available else "unverified",
                    "availability_reviewed" if available else "availability_unknown",
                    self.ref(obs, index),
                )
        self.identities()
        self.context()
        self.results()
        for obs in self.by_source["nbbet_stats"]:
            statistics(self, obs)
        for obs in self.by_source["pbp"]:
            point_by_point(self, obs)
        # Several checks can encounter the same bad source row. Keep one decision per key.
        self.tables["data_quality"] = list(
            {r["quality_id"]: r for r in self.tables["data_quality"]}.values()
        )
        return self.tables
