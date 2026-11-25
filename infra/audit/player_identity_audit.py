"""Bounded read-only S3 identity evidence; no application imports or DB."""
import collections
import functools
import hashlib
import importlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import signal
import time
from urllib.parse import urlsplit

ROOTS = ("nbbet_details/", "nbbet_raw/")
MAX_LISTS, MAX_GETS, MAX_FILE = 32, 24, 262144
MAX_BYTES, MAX_ROWS, MAX_OUTPUT = 12*1024*1024, 6144, 32768
FIELDS = ("S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")


STAGES = frozenset(("configuration", "dependency", "resource", "list", "get", "decode", "evidence", "output"))
REASONS = frozenset(("validation_failed", "missing_setting", "unsupported_schema",
    "budget_exhausted", "timeout", "access_denied", "not_found", "object_changed",
    "dependency_unavailable", "resource_exhausted", "transport_error", "service_error",
    "internal_error", "output_limit"))


class Stop(Exception):
    pass


class Diagnostic(Stop):
    def __init__(self, stage, reason):
        self.stage = stage if stage in STAGES else "resource"
        self.reason = reason if reason in REASONS else "internal_error"
        super().__init__()


def reason(exc):
    if isinstance(exc, Diagnostic):
        return exc.reason if exc.reason in REASONS else "internal_error"
    if isinstance(exc, Stop):
        return exc.args[0] if exc.args and isinstance(exc.args[0], str) and exc.args[0] in REASONS else "validation_failed"
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "dependency_unavailable"
    if isinstance(exc, PermissionError):
        return "access_denied"
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if type(exc).__name__ in ("ArrowInvalid", "ArrowNotImplementedError"):
        return "unsupported_schema"
    if isinstance(exc, MemoryError):
        return "resource_exhausted"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if type(exc).__name__ in ("ReadTimeoutError", "ConnectTimeoutError"):
        return "timeout"
    if type(exc).__name__ in ("EndpointConnectionError", "ConnectionClosedError"):
        return "transport_error"
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status in (401, 403):
            return "access_denied"
        if status == 404:
            return "not_found"
        if status == 412:
            return "object_changed"
        return "service_error"
    return "internal_error"


def run_stage(stage, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Diagnostic:
        raise
    except Exception as exc:
        raise Diagnostic(stage, reason(exc)) from None


def phase(stage):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            return run_stage(stage, fn, *args, **kwargs)
        return wrapped
    return decorate


def failure_report(exc):
    stage = exc.stage if isinstance(exc, Diagnostic) and exc.stage in STAGES else "resource"
    return {"version": 1, "complete": False, "bounded_scan_finished": False,
            "error": "identity_audit_unavailable", "stage": stage, "reason": reason(exc)}


def require(ok, code='validation_failed'):
    if not ok:
        raise Stop(code)


@phase('configuration')
def config(raw):
    require(len(raw) <= 65536)
    values = {}
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if not m:
            require(not any(re.match(r"(?:export\s+)?" + k + r"\b", line) for k in FIELDS))
            continue
        k, v = m.groups()
        if k not in FIELDS:
            continue
        require(k not in values)
        v = v.strip()
        if v.startswith(("'", '"')):
            end = v.find(v[0], 1)
            require(end > 0 and (not v[end+1:].strip() or v[end+1:].lstrip().startswith("#")))
            v = v[1:end]
        else:
            require(re.fullmatch(r"""[^\s#"'\\]+""", v) is not None)
        require(0 < len(v) <= 4096 and not any(c in v for c in ("\x00", "\\", "$", chr(96))))
        values[k] = v
    require(all(values.get(k) for k in FIELDS), "missing_setting")
    require(values["S3_ENDPOINT_URL"] == "https://storage.yandexcloud.net")
    require(values["S3_REGION"] == "ru-central1")
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", values["S3_BUCKET"]) is not None)
    require(".." not in values["S3_BUCKET"])
    try:
        ipaddress.ip_address(values["S3_BUCKET"])
    except ValueError:
        return values
    raise Stop()


def token(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).digest()


class Evidence:
    def __init__(self):
        self.rows = self.invalid = self.nodes = self.missing_players = 0
        self.objects = collections.Counter()
        self.paths, self.seen_rows = {}, set()

    def walk(self, obj, path=(), depth=0):
        self.nodes += 1
        require(self.nodes <= 300000 and depth <= 10)
        if isinstance(obj, dict):
            require(len(obj) <= 128)
            for k, v in obj.items():
                label = "field_" + k if k in {str(i) for i in range(65)} else "*"
                yield from self.walk(v, path + (label,), depth+1)
        elif isinstance(obj, list):
            require(len(obj) <= 128)
            for v in obj:
                yield from self.walk(v, path + ("[]",), depth+1)
        elif obj is None or type(obj) in (str, int, float, bool):
            require(not isinstance(obj, str) or len(obj.encode()) <= 16384)
            if type(obj) in (str, int) and obj != "":
                yield ".".join(path), obj

    @phase('evidence')
    def observe(self, source, year, gender, row):
        require(self.rows < MAX_ROWS)
        self.rows += 1
        text = row.get("players_json" if source == "details" else "raw_json")
        if not isinstance(text, str) or len(text.encode()) > 65536:
            self.invalid += 1
            return
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError):
            self.invalid += 1
            return
        if not isinstance(obj, dict):
            self.invalid += 1
            return
        signature = (source, year, gender, token(obj))
        if signature in self.seen_rows:
            return
        self.seen_rows.add(signature)
        players = []
        if source == "details":
            players = [("details.player", obj.get(side)) for side in ("p1", "p2")]
        else:
            match = obj.get("match")
            if isinstance(match, dict):
                players.extend(("raw.match.player", match.get(k)) for k in ("3", "5"))
            def outside(value, path="raw.outside", depth=0):
                self.nodes += 1
                require(self.nodes <= 300000 and depth <= 8)
                if isinstance(value, dict):
                    require(len(value) <= 128)
                    if isinstance(value.get("1"), str):
                        players.append((path, value))
                    for mapping_key, child in value.items():
                        if (isinstance(child, dict) and isinstance(child.get("1"), str)
                                and isinstance(mapping_key, str) and len(mapping_key) <= 128):
                            players.append((path + ".mapping_key", {"1": child["1"], "64": mapping_key}))
                        outside(child, path + ".*", depth+1)
                elif isinstance(value, list):
                    require(len(value) <= 128)
                    for child in value:
                        outside(child, path + ".[]", depth+1)
            for k, value in obj.items():
                if k != "match":
                    outside(value)
        require(len(players) <= 128)
        for scope, player in players:
            if not isinstance(player, dict):
                self.missing_players += 1
                continue
            self.objects[scope] += 1
            name = player.get("1")
            name_hash = token(name.strip().casefold()) if isinstance(name, str) and name.strip() else None
            seen_paths = set()
            for path, value in self.walk(player):
                key = (scope, path, type(value).__name__)
                require(key in self.paths or len(self.paths) < 256)
                e = self.paths.setdefault(key, {"n": 0, "objects": 0, "values": {}, "names": {}})
                e["n"] += 1
                if key not in seen_paths:
                    e["objects"] += 1
                    seen_paths.add(key)
                h = token((type(value).__name__, value))
                require(h in e["values"] or len(e["values"]) < MAX_ROWS*2)
                v = e["values"].setdefault(h, {"n": 0, "years": set(), "names": set(), "genders": set()})
                v["n"] += 1
                v["years"].add(year)
                v["genders"].add(gender)
                if name_hash:
                    v["names"].add(name_hash)
                    e["names"].setdefault(name_hash, set()).add(h)

    @phase('evidence')
    def report(self):
        paths = []
        for (scope, path, kind), e in sorted(self.paths.items()):
            vals = list(e["values"].values())
            known = scope in ("details.player", "raw.match.player") and (
                path in ("field_1", "field_2", "field_3") or path.startswith("field_4."))
            paths.append({
                "scope": scope, "path": path, "type": kind,
                "classification": "known_name_photo_flag_or_rank" if known else "unverified_candidate",
                "observations": e["n"], "objects_with_path": e["objects"],
                "player_objects_denominator": self.objects[scope],
                "distinct_values": len(vals), "repeated_values": sum(v["n"] > 1 for v in vals),
                "cross_year_values": sum(len(v["years"]) > 1 for v in vals),
                "cross_year_single_name_values": sum(len(v["years"]) > 1 and len(v["names"]) == 1 for v in vals),
                "values_with_multiple_names": sum(len(v["names"]) > 1 for v in vals),
                "names_with_multiple_values": sum(len(v) > 1 for v in e["names"].values()),
                "cross_gender_values": sum(len(v["genders"]) > 1 for v in vals),
                "name_association": "field_1_heuristic_not_verified"})
        return {"decoded_rows": self.rows, "invalid_json_rows": self.invalid,
                "missing_player_objects": self.missing_players,
                "unique_source_year_rows": len(self.seen_rows), "paths": paths,
                "identity_confirmed": False, "semantic_verification": "pending"}


class Reader:
    def __init__(self, client, bucket, clock=time.monotonic):
        self.client, self.bucket, self.clock = client, bucket, clock
        self.end = clock() + 65
        self.lists = self.gets = self.bytes = self.attempts = self.sent_in_call = 0

    def check(self):
        require(self.clock() < self.end, "timeout")

    def guard(self, model, params, **kwargs):
        require(params.get("Bucket") == self.bucket)
        if model.name == "ListObjectsV2":
            require(params.get("MaxKeys") == 128 and params.get("Delimiter", "/") == "/")
            require(re.fullmatch(r"nbbet_(?:details|raw)/gender=[MF]/level=(?:tour|challenger)/(?:year=[0-9]{4}/)?",
                                 params.get("Prefix", "")) is not None)
        elif model.name == "GetObject":
            require(re.fullmatch(r"nbbet_(?:details|raw)/gender=[MF]/level=(?:tour|challenger)/year=[0-9]{4}/[^/]+\.parquet",
                                 params.get("Key", "")) is not None)
            require(bool(params.get("IfMatch")))
        else:
            raise Stop()

    def before_send(self, request, **kwargs):
        self.check()
        target = urlsplit(request.url)
        require(target.scheme == "https" and target.hostname == "storage.yandexcloud.net"
                and target.port in (None, 443) and not target.username and not target.password)
        require(self.attempts < MAX_LISTS + MAX_GETS, "budget_exhausted")
        require(self.sent_in_call == 0, "budget_exhausted")
        self.sent_in_call += 1
        self.attempts += 1

    def call(self, op, **params):
        self.check()
        class Model:
            name = {"list_objects_v2": "ListObjectsV2", "get_object": "GetObject"}.get(op)
        self.guard(Model, {"Bucket": self.bucket, **params})
        if op == "list_objects_v2":
            require(self.lists < MAX_LISTS, "budget_exhausted")
            self.lists += 1
        else:
            require(self.gets < MAX_GETS, "budget_exhausted")
            self.gets += 1
        self.sent_in_call = 0
        return getattr(self.client, op)(Bucket=self.bucket, **params)

    @phase('get')
    def get(self, item):
        size = item.get("Size")
        require(type(size) is int and 12 <= size <= MAX_FILE and self.bytes + size <= MAX_BYTES)
        require(isinstance(item.get("ETag"), str) and 0 < len(item["ETag"]) < 256)
        self.bytes += size
        response = self.call("get_object", Key=item["Key"], IfMatch=item["ETag"])
        stream = response.get("Body")
        try:
            require(response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200)
            require(response.get("ContentLength") == size and response.get("ETag") == item["ETag"])
            data = stream.read(size)
            self.check()
            require(len(data) == size)
            return data
        finally:
            if stream:
                stream.close()

    @phase('list')
    def page(self, prefix, years=False):
        require(self.bytes + 1024*1024 <= MAX_BYTES, "budget_exhausted")
        page = self.call("list_objects_v2", Prefix=prefix, MaxKeys=128, **({"Delimiter": "/"} if years else {}))
        require(type(page.get("IsTruncated")) is bool)
        require(len(page.get("Contents", [])) <= 128 and len(page.get("CommonPrefixes", [])) <= 128)
        self.bytes += len(json.dumps(page, default=str).encode()) * 6
        require(self.bytes <= MAX_BYTES, "budget_exhausted")
        self.check()
        return page


@phase('decode')
def decode(data, field):
    import pyarrow as pa
    import pyarrow.parquet as pq
    require(data[:4] == b"PAR1" and data[-4:] == b"PAR1")
    f = pq.ParquetFile(pa.BufferReader(data), thrift_string_size_limit=262144,
                       thrift_container_size_limit=4096)
    m = f.metadata
    require(m.num_rows <= 256 and m.num_columns <= 64 and m.num_row_groups <= 16)
    require(sum(m.row_group(i).total_byte_size for i in range(m.num_row_groups)) <= 4*1024*1024)
    require(field in f.schema_arrow.names, "unsupported_schema")
    dtype = f.schema_arrow.field(field).type
    require(pa.types.is_string(dtype) or pa.types.is_large_string(dtype))
    table = f.read(columns=[field], use_threads=False)
    require(table.nbytes <= 4*1024*1024)
    return table.to_pylist()


def collect(reader):
    evidence, strata = Evidence(), []
    for root in ROOTS:
        for gender in ("M", "F"):
            for level in ("tour", "challenger"):
                base = root + "gender=" + gender + "/level=" + level + "/"
                page = reader.page(base, years=True)
                years = []
                for p in page.get("CommonPrefixes", []):
                    m = re.fullmatch(re.escape(base) + r"year=([0-9]{4})/", p.get("Prefix", ""))
                    require(m is not None and 2010 <= int(m[1]) <= 2100)
                    years.append(int(m[1]))
                years = sorted(set(years))
                picks = sorted({years[i*(len(years)-1)//max(1,min(3,len(years))-1)]
                                for i in range(min(3,len(years)))})
                strata.append({"source": root[:-1], "gender": gender, "level": level,
                               "observed_years": years, "selected_years": picks,
                               "year_listing_truncated": page["IsTruncated"]})
                for year in picks:
                    prefix = base + "year=" + str(year) + "/"
                    page = reader.page(prefix)
                    candidates, skipped = [], 0
                    for o in page.get("Contents", []):
                        key = o.get("Key")
                        require(isinstance(key, str) and key.startswith(prefix) and len(key.encode()) <= 1024)
                        if key.endswith(".parquet") and type(o.get("Size")) is int and 12 <= o["Size"] <= MAX_FILE:
                            candidates.append(o)
                        else:
                            skipped += 1
                    candidates.sort(key=lambda o: o["Key"])
                    item = {"source": root[:-1], "gender": gender, "level": level, "year": year,
                            "listing_truncated": page["IsTruncated"], "eligible_observed": len(candidates),
                            "ineligible_observed": skipped, "files_read": 0, "decoded_rows": 0}
                    strata.append(item)
                    if candidates:
                        field = "players_json" if root == "nbbet_details/" else "raw_json"
                        for row in decode(reader.get(candidates[len(candidates)//2]), field):
                            reader.check()
                            evidence.observe("details" if field == "players_json" else "raw", year, gender, row)
                            item["decoded_rows"] += 1
                        item["files_read"] = 1
    reader.check()
    report = evidence.report()
    report.update({"version": 1, "complete": False, "bounded_scan_finished": True,
                   "sampling": "observed_year_extremes_middle_one_small_lexical_middle_file_per_stratum",
                   "population_coverage": "unknown", "strata": strata,
                   "budget": {"lists": reader.lists, "gets": reader.gets, "accounted_bytes": reader.bytes},
                   "unknowns": ["source_id_semantics", "unseen_years_keys_large_files",
                                "repeated_matches_not_independent", "mapping_key_identity_not_verified"]})
    return report


@phase('output')
def encode(report):
    result = json.dumps(report, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    require(len(result.encode()) <= MAX_OUTPUT, "output_limit")
    return result


@phase("resource")
def resource_limits():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (4*1024**3, 4*1024**3))


@phase("configuration")
def read_config():
    with Path("/root/Tennis_pelmen_model/.env").open("rb") as stream:
        return config(stream.read(65537))


@phase("dependency")
def dependencies():
    for module in ("boto3", "botocore.config", "pyarrow", "pyarrow.parquet"):
        importlib.import_module(module)


@phase("configuration")
def make_client(values):
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=values["S3_ENDPOINT_URL"],
        region_name=values["S3_REGION"], aws_access_key_id=values["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=values["S3_SECRET_ACCESS_KEY"],
        config=Config(connect_timeout=2, read_timeout=3,
            retries={"total_max_attempts": 1, "mode": "standard"}, s3={"addressing_style": "path"}))


def main():
    logging.disable(logging.CRITICAL)
    try:
        run_stage("resource", signal.signal, signal.SIGALRM, lambda *_: os._exit(124))
        run_stage("resource", signal.alarm, 95)
        resource_limits()
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        os.environ["OMP_NUM_THREADS"] = "1"
        dependencies()
        values = read_config()
        client = make_client(values)
        reader = Reader(client, values["S3_BUCKET"])
        run_stage("configuration", client.meta.events.register, "before-parameter-build.s3", reader.guard)
        run_stage("configuration", client.meta.events.register, "before-send.s3", reader.before_send)
        report = run_stage("evidence", collect, reader)
        print(encode(report))
        return 0
    except Exception as exc:
        # Construct output exclusively from fixed enums, never exception text.
        print(json.dumps(failure_report(exc), separators=(",", ":")))
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
