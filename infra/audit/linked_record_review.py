"""Separate bounded linked review projection. No connection creation or production CLI.

Only synthetic examples may be committed. Production integration needs separate approval.
Existing raw/full-record exporters are deliberately not imported.
"""
from collections import Counter
from contextlib import contextmanager
import io
import json
import math
import re
import signal
import time
import uuid
from urllib.parse import urlsplit

SOURCES = ("nbbet_details", "nbbet_stats", "pbp", "nbbet_raw")
MAX_MATCHES, MAX_OBJECTS, MAX_CALLS = 3, 12, 24
MAX_FILE, MAX_BYTES, MAX_ROWS = 256*1024, 8*1024*1024, 12000
MAX_OUTPUT, MAX_PER_SOURCE = 24000, 6
NUMERIC_SET_KEYS = frozenset(str(i) for i in range(1, 9))
STAT_BASE = ("aces", "double_faults", "first_serve_points_all", "first_serve_points_won",
             "second_serve_points_all", "second_serve_points_won", "break_points_won",
             "break_points_all", "winners", "unforced_errors", "max_points_in_row",
             "max_games_in_row", "service_games_won", "service_games_all")
STATS = frozenset(f"{name}_p{p}" for name in STAT_BASE for p in (1, 2))
PBP_NUMBERS = frozenset(("set_num", "game_num", "point_num", "server", "point_winner"))
PBP_BOOLS = frozenset(("server_won_point", "is_break_point", "is_match_point", "is_tiebreak", "game_complete"))
FIELDS = {
    "nbbet_details": frozenset(("status", "has_coverage", "details_version", "sets_json", "players_json")),
    "nbbet_stats": STATS | {"scope"},
    "pbp": PBP_NUMBERS | PBP_BOOLS | {"score_server", "score_returner", "point_type"},
    "nbbet_raw": frozenset(("raw_json",)),
}
CASES = frozenset(("finished_straight", "deciding_tiebreak", "bo5", "retirement", "walkover", "legacy", "current"))


class ReviewError(ValueError):
    """Fixed allowlisted diagnostic only."""


def require(ok, code="invalid_input"):
    if not ok:
        raise ReviewError(code)


def match_id(value):
    try:
        require(isinstance(value, (str, uuid.UUID)), "invalid_match_id")
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ReviewError("invalid_match_id") from None


def parse_json(raw):
    require(type(raw) is str and len(raw.encode()) <= 4096, "json_limit")
    def unique(pairs):
        result = {}
        for k, v in pairs:
            require(k not in result, "duplicate_json_key")
            result[k] = v
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise ReviewError("invalid_json") from None


def structural_sets(raw):
    """Reviewed *separate* allowlist, numeric keys 1..8 only inside set structures."""
    sets = parse_json(raw) if type(raw) is str else raw
    require(type(sets) is list and len(sets) <= 5, "set_shape_limit")
    nodes = [0]
    def visit(value, depth):
        nodes[0] += 1
        require(nodes[0] <= 256 and depth <= 3, "set_shape_limit")
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            return value if 0 <= value <= 99 else {"redacted": "integer"}
        if type(value) is float:
            return value if math.isfinite(value) and 0 <= value <= 99 else {"redacted": "number"}
        if type(value) is str:
            return value if re.fullmatch(r"(?:0|[1-9][0-9]?)", value) else {"redacted": "string"}
        if type(value) is list:
            require(len(value) <= 8, "set_shape_limit")
            return [visit(v, depth+1) for v in value]
        if type(value) is dict:
            require(len(value) <= 8, "set_shape_limit")
            return {"fields": {k: visit(v, depth+1) for k, v in value.items() if k in NUMERIC_SET_KEYS},
                    "omitted_keys": sum(k not in NUMERIC_SET_KEYS for k in value)}
        return {"redacted": "type"}
    return [visit(s, 0) for s in sets]


def primitive(value, *, boolean=False, side=False):
    if value is None:
        return None
    if boolean:
        return value if type(value) is bool else {"redacted": "type"}
    if (type(value) is int and abs(value) <= 10000000) or (type(value) is float and math.isfinite(value) and abs(value) <= 10000000):
        if side and (type(value) is not int or value not in (1, 2)):
            return {"redacted": "side"}
        return value
    return {"redacted": "type"}


def project(source, row, player_aliases):
    require(source in SOURCES and type(row) is dict)
    out = {}
    for key, value in row.items():
        if key not in FIELDS[source]:
            continue
        if source == "nbbet_raw":
            if value is None:
                out[key] = None
                continue
            if type(value) is str and len(value.encode()) > 4096:
                out[key] = {"omitted": "json_limit"}
                continue
            raw = parse_json(value)
            require(type(raw) is dict, "invalid_raw_shape")
            match = raw.get("match")
            require(type(match) is dict, "invalid_raw_shape")
            projected = {}
            if "23" in match:
                status = match["23"]
                projected["23"] = status if type(status) is int and status in range(1, 11) else None
            if "24" in match:
                projected["24"] = structural_sets(match["24"])
            for slot, index in (("3", 0), ("5", 1)):
                if slot in match:
                    projected[slot] = {"player_alias": player_aliases[index] if player_aliases else None,
                                       "profile_values_redacted": True}
            out[key] = {"match": projected, "outside_match_keys_omitted": len(raw)-1}
        elif key == "sets_json":
            out[key] = None if value is None else structural_sets(value)
        elif key == "players_json":
            # Never export even numeric keys from a player profile.
            players = parse_json(value) if value is not None else None
            require(players is None or type(players) is dict, "invalid_players_shape")
            out[key] = None if players is None else {
                side: {"player_alias": player_aliases[i] if player_aliases else None,
                       "profile_values_redacted": True}
                for i, side in enumerate(("p1", "p2")) if side in players}
        elif key in ("score_server", "score_returner"):
            out[key] = value if value is None or (type(value) is str and re.fullmatch(r"(?:0|15|30|40|AD|A|[0-9]{1,2})", value)) else {"redacted": "string"}
        elif key == "point_type":
            out[key] = value if value in ("ace", "double_fault", "winner", "unforced_error", None) else {"redacted": "string"}
        elif key in PBP_BOOLS or key == "has_coverage":
            out[key] = primitive(value, boolean=True)
        else:
            out[key] = primitive(value, side=key in ("server", "point_winner"))
    return out


def exact_keys(pbp_path):
    """Only current detail_job part suffix; candidates, never existence proof."""
    if type(pbp_path) is not str:
        return None
    m = re.fullmatch(r"pbp/(gender=[MF]/level=(?:tour|challenger)/year=[0-9]{4}/part-[A-Za-z0-9_-]+\.parquet)", pbp_path)
    return {s: s+"/"+m[1] for s in SOURCES} if m else None


def validate_plan(plan):
    require(type(plan) is dict and 1 <= len(plan) <= MAX_MATCHES, "selection_limit")
    keys = set()
    for mid, mapping in plan.items():
        require(match_id(mid) == mid)
        require(type(mapping) is dict and set(mapping) <= set(SOURCES))
        for source, key in mapping.items():
            require(type(key) is str and len(key.encode()) <= 1024 and re.fullmatch(
                re.escape(source)+r"/gender=[MF]/level=(?:tour|challenger)/year=[0-9]{4}/part-[A-Za-z0-9_-]+\.parquet", key), "invalid_object_plan")
            keys.add((source, key))
    require(len(keys) <= MAX_OBJECTS, "object_limit")
    return sorted(keys)


class Budget:
    def __init__(self, clock=time.monotonic):
        self.clock, self.end = clock, clock()+65
        self.calls = self.bytes = self.rows = self.http = self.sent = 0
    def check(self):
        require(self.clock() < self.end, "deadline")
    def charge(self, size=0):
        self.check()
        require(self.calls < MAX_CALLS and self.bytes+size <= MAX_BYTES, "request_budget")
        self.calls += 1
        self.bytes += size
        self.sent = 0


class ObjectReader:
    def __init__(self, client, bucket, plan, budget):
        require(type(bucket) is str and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket), "invalid_bucket")
        self.client, self.bucket, self.budget = client, bucket, budget
        self.keys = validate_plan(plan)
        client.meta.events.register("before-parameter-build.s3", self.guard)
        client.meta.events.register("before-send.s3", self.before_send)

    def guard(self, model, params, **kwargs):
        require(model.name in ("HeadObject", "GetObject") and params.get("Bucket") == self.bucket, "operation_forbidden")
        require(any(params.get("Key") == key for _, key in self.keys), "operation_forbidden")
        if model.name == "GetObject":
            require(bool(params.get("IfMatch")), "conditional_read_required")

    def before_send(self, request, **kwargs):
        self.budget.check()
        url = urlsplit(request.url)
        require(url.scheme == "https" and url.hostname == "storage.yandexcloud.net" and
                url.port in (None, 443) and not url.username and not url.password, "destination_forbidden")
        require(self.budget.sent == 0 and self.budget.http < MAX_CALLS, "retry_forbidden")
        self.budget.sent += 1
        self.budget.http += 1

    def call(self, operation, **params):
        class Model:
            name = {"head_object": "HeadObject", "get_object": "GetObject"}.get(operation)
        self.guard(Model, {"Bucket": self.bucket, **params})
        return getattr(self.client, operation)(Bucket=self.bucket, **params)

    def read(self, source, key):
        import pyarrow as pa
        import pyarrow.parquet as pq
        require((source, key) in self.keys, "operation_forbidden")
        self.budget.charge()
        try:
            head = self.call("head_object", Key=key)
        except Exception as exc:
            if getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise ReviewError("object_unavailable") from None
        size, tag = head.get("ContentLength"), head.get("ETag")
        require(type(size) is int and 12 <= size <= MAX_FILE and type(tag) is str and 0 < len(tag) < 256, "object_limit")
        self.budget.charge(size)
        response = self.call("get_object", Key=key, IfMatch=tag)
        body = response.get("Body")
        try:
            require(response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200 and
                    response.get("ContentLength") == size and response.get("ETag") == tag, "object_changed")
            data = body.read(size)
            self.budget.check()
            require(len(data) == size and data[:4] == b"PAR1" and data[-4:] == b"PAR1", "invalid_parquet")
        finally:
            if body is not None:
                body.close()
        try:
            with pq.ParquetFile(pa.BufferReader(data), thrift_string_size_limit=262144,
                                thrift_container_size_limit=4096) as f:
                m = f.metadata
                require(m.num_rows <= 2048 and m.num_columns <= 64 and m.num_row_groups <= 32, "decode_limit")
                require(self.budget.rows + m.num_rows <= MAX_ROWS and
                        sum(m.row_group(i).total_byte_size for i in range(m.num_row_groups)) <= 4*1024*1024, "decode_limit")
                self.budget.rows += m.num_rows
                names = f.schema_arrow.names
                require("canonical_match_id" in names and len(names) == len(set(names)), "join_schema_missing")
                selected = ["canonical_match_id"] + [x for x in names if x in FIELDS[source]]
                table = f.read(columns=selected, use_threads=False)
                require(table.nbytes <= 4*1024*1024, "decode_limit")
                rows = table.to_pylist()
                types = {}
                for field in table.schema:
                    if field.name == "canonical_match_id":
                        continue
                    dtype = field.type
                    types[field.name] = ("null" if pa.types.is_null(dtype) else
                        "bool" if pa.types.is_boolean(dtype) else
                        "int" if pa.types.is_integer(dtype) else "float" if pa.types.is_floating(dtype) else
                        "string" if pa.types.is_string(dtype) or pa.types.is_large_string(dtype) else "other")
        except ReviewError:
            raise
        except Exception:
            raise ReviewError("decode_failed") from None
        self.budget.check()
        return {"rows": rows, "storage_types": types}


def read_matches(connection, ids):
    """Exact PK lookup only; requires already configured SELECT-only timed session."""
    require(type(ids) is list and 1 <= len(ids) <= MAX_MATCHES and len(set(ids)) == len(ids), "selection_limit")
    ids = [match_id(x) for x in ids]
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('transaction_read_only'), current_setting('statement_timeout') LIMIT 1")
        settings = cursor.fetchmany(2)
        require(len(settings) == 1 and settings[0][0] == "on", "readonly_required")
        timeout = settings[0][1]
        m = re.fullmatch(r"([0-9]+)(ms|s)?", timeout)
        require(m is not None and 0 < int(m[1])*(1000 if m[2] == "s" else 1) <= 3000, "sql_timeout_required")
        cursor.execute("SELECT match_id::text, gender, level, status, surface, duplicate_of, pbp_s3_path "
                       "FROM public.matches WHERE match_id = ANY(%s::uuid[]) LIMIT 3", (ids,))
        rows = cursor.fetchmany(4)
        require(len(rows) <= MAX_MATCHES, "sql_row_limit")
    result = {}
    for row in rows:
        mid = match_id(row[0])
        require(mid in ids and mid not in result, "join_conflict")
        result[mid] = dict(zip(("match_id", "gender", "level", "status", "surface", "duplicate_of", "pbp_s3_path"), row))
    return result


def build_review(matches, objects, plan, *, orientations=None, independent_winners=None, requested_cases=()):
    validate_plan(plan)
    require(type(matches) is dict and len(matches) <= MAX_MATCHES and set(matches) <= set(plan))
    require(type(objects) is dict and len(objects) <= MAX_OBJECTS)
    total_rows = 0
    for obj in objects.values():
        if obj is not None:
            require(type(obj) is dict and type(obj.get("rows")) is list and len(obj["rows"]) <= 2048, "decode_limit")
            total_rows += len(obj["rows"])
    require(total_rows <= MAX_ROWS, "decode_limit")
    require(type(requested_cases) in (list, tuple) and len(requested_cases) <= len(CASES) and set(requested_cases) <= CASES, "unknown_case")
    orientations, independent_winners = orientations or {}, independent_winners or {}
    require(type(orientations) is dict and len(orientations) <= MAX_MATCHES*len(SOURCES), "orientation_unreviewed")
    require(type(independent_winners) is dict and len(independent_winners) <= MAX_MATCHES and set(independent_winners) <= set(plan), "winner_unreviewed")
    report = {"version": "linked-sanitized-v1", "complete": False, "population_coverage": "unknown",
              "semantic_verification": "pending", "matches": [],
              "requested_cases": list(requested_cases), "unconfirmed_cases": list(requested_cases),
              "case_coverage": "manual_review_required"}
    for index, mid in enumerate(plan, 1):
        row = matches.get(mid)
        require(row is None or (type(row) is dict and row.get("match_id") == mid), "join_conflict")
        entry = {"match_alias": "M"+str(index), "player_aliases": ["M"+str(index)+"P1", "M"+str(index)+"P2"],
                 "player_identity": "local_roles_not_verified_people", "matches": None, "sources": {},
                 "independent_winner": {"available": False, "reason": "no_reviewed_reference"}}
        if row is not None:
            require("duplicate_of" in row and row["duplicate_of"] is None and row.get("gender") in ("M", "F") and row.get("level") in ("tour", "challenger"), "scope_or_duplicate")
            entry["matches"] = {"gender": row["gender"], "level": row["level"],
                "status": row.get("status") if type(row.get("status")) is int and row["status"] in range(1,11) else None,
                "surface": row.get("surface") if row.get("surface") in ("hard", "clay", "grass", "carpet", None) else None}
        for source in SOURCES:
            key = plan[mid].get(source)
            obj = objects.get((source, key)) if key is not None else None
            if obj is None:
                entry["sources"][source] = {"coverage": "missing_or_unplanned"}
                continue
            orient = orientations.get((mid, source))
            aliases = None
            if orient is not None:
                require(type(orient) is dict and set(orient) == {"order", "reviewed", "reference"} and orient.get("order") in ("same", "swapped") and
                        orient.get("reviewed") is True and type(orient.get("reference")) is str and
                        0 < len(orient["reference"]) <= 512, "orientation_unreviewed")
                aliases = entry["player_aliases"] if orient["order"] == "same" else entry["player_aliases"][::-1]
            linked = []
            total = invalid_joins = 0
            for candidate in obj["rows"]:
                require(type(candidate) is dict, "invalid_row")
                try:
                    cid = match_id(candidate.get("canonical_match_id"))
                except ReviewError:
                    invalid_joins += 1
                    continue
                if cid == mid:
                    total += 1
                    if len(linked) < MAX_PER_SOURCE:
                        linked.append(project(source, candidate, aliases))
            types = obj.get("storage_types", {})
            require(type(types) is dict and set(types) <= FIELDS[source] and
                    all(v in ("null", "bool", "int", "float", "string", "other") for v in types.values()), "invalid_schema_summary")
            entry["sources"][source] = {
                "coverage": "observed_subset" if total else "no_matching_rows_in_object",
                "matching_rows_observed": total, "invalid_join_rows": invalid_joins, "rows_omitted_by_cap": max(0,total-len(linked)),
                "orientation": "caller_review_attested" if aliases else "unknown",
                "source_player_aliases": aliases, "storage_types": types,
                "absent_columns": sorted(FIELDS[source]-types.keys()), "rows": linked}
        evidence = independent_winners.get(mid)
        if evidence is not None:
            require(type(evidence) is dict and set(evidence) == {"basis", "reviewed", "side", "reference"} and evidence.get("basis") == "independent_reference" and
                    evidence.get("reviewed") is True and type(evidence.get("side")) is int and
                    evidence["side"] in (1,2) and type(evidence.get("reference")) is str and
                    0 < len(evidence["reference"]) <= 512, "winner_unreviewed")
            entry["independent_winner"] = {"available": True,
                "winner_alias": entry["player_aliases"][evidence["side"]-1],
                "basis": "caller_attested_independent_reference", "verified_by_exporter": False}
        report["matches"].append(entry)
    return report


def encode(report):
    data = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    require(len(data) <= MAX_OUTPUT, "output_limit")
    return data


@contextmanager
def isolated_limits():
    """Use only in a dedicated approved local/remote process, main thread."""
    import resource
    old_limit = resource.getrlimit(resource.RLIMIT_AS)
    old_handler = signal.getsignal(signal.SIGALRM)
    old_alarm = signal.alarm(0)
    start = time.monotonic()
    cap = min(4*1024**3, old_limit[0]) if old_limit[0] != resource.RLIM_INFINITY else 4*1024**3
    try:
        resource.setrlimit(resource.RLIMIT_AS, (cap, old_limit[1]))
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(ReviewError("hard_deadline")))
        signal.alarm(min(95, old_alarm) if old_alarm else 95)
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        resource.setrlimit(resource.RLIMIT_AS, old_limit)
        if old_alarm:
            signal.alarm(max(1, old_alarm-int(time.monotonic()-start)))


def review_from_clients(connection, client, bucket, plan, **options):
    """No credentials/config/default paths; no output until whole projection succeeds."""
    try:
        with isolated_limits():
            budget = Budget()
            keys = validate_plan(plan)
            matches = read_matches(connection, list(plan))
            budget.check()
            reader = ObjectReader(client, bucket, plan, budget)
            objects = {(source, key): reader.read(source, key) for source, key in keys}
            report = build_review(matches, objects, plan, **options)
            report["budget"] = {"calls": budget.calls, "http_attempts": budget.http, "get_bytes_reserved": budget.bytes, "decoded_rows": budget.rows}
            budget.check()
            return encode(report)
    except Exception:
        # Caller can report only this fixed code; never print traceback/source data.
        raise ReviewError("linked_review_unavailable") from None
