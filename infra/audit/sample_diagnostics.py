"""Fixed diagnostic protocol. Never serialize exceptions, identifiers or paths."""
from contextlib import contextmanager
import sys

STAGES = frozenset(("runner_preflight","runner_request","plan_operation","operation_batch","catalog_progress","publication_validation","github_publication","host_scan","host_verify",
    "ssh_transport","remote_entry","remote_runtime","remote_bootstrap","remote_dependencies",
    "remote_config","dependency_psycopg2","dependency_boto3","dependency_pyarrow","postgres_connect","postgres_close","postgres_session","postgres_selection","postgres_rows",
    "source_manifest","s3_client","s3_read","s3_head","s3_get","parquet_decode",
    "content_validation","catalog_write","archive","private_staging","stage_release",
    "stage_cleanup","runner_capture","diagnostic_channel"))
REASONS = frozenset(("none","invalid_input","tool_missing","python_version","ssh_settings_missing",
    "root_policy","fingerprint_invalid","project_invalid","request_failed","discovery_failed",
    "timeout","key_count","key_format","fingerprint_mismatch","auth_or_transport_failed",
    "auth_denied","dns_failed","connect_failed","key_invalid","host_key_failed","remote_exit",
    "project_unavailable","python_unavailable","timeout_missing","payload_invalid","code_staging",
    "imports_failed","dependency_missing","dependency_version","config_missing","config_invalid",
    "permission_denied","schema_missing","query_timeout","lock_timeout","connection_failed",
    "readonly_required","sql_timeout_required","selection_plan","selection_empty","selection_invalid",
    "row_limit","byte_limit","scope_changed","manifest_invalid","object_missing","object_changed",
    "s3_head_failed","s3_get_failed","decode_failed","decode_limit","sensitive_content",
    "content_invalid","output_limit","stage_capacity","stage_expired","stage_changed",
    "stage_private","review_required","malformed_output","digest_mismatch","capture_failed",
    "upstream_failed","pending_sample_pr","first20_complete","ambiguous_plan","catalog_invalid","unexpected"))
STATUSES = frozenset(("start","ok","error","skipped"))


def emit(stage, status, reason="none", stream=None):
    if stage not in STAGES or status not in STATUSES or reason not in REASONS:
        raise ValueError("invalid_diagnostic")
    print("sample_diag:"+stage+":"+status+":"+reason,file=stream or sys.stderr,flush=True)


def reported(exc):
    try: return getattr(exc,"_sample_diagnostic_reported",False) is True
    except Exception: return False


def reason(stage, exc):
    # Exact equality against known codes; arbitrary exception text is never emitted.
    try:
        args=getattr(exc,"args",())
        code=args[0] if len(args)==1 and type(args[0])is str else ""
        fixed={
            "operation_limit":"output_limit","operation_archive_limit":"byte_limit",
            "operation_archive_changed":"stage_changed","operation_batch_failed":"upstream_failed",
            "operation_slice_invalid":"content_invalid","operation_plan_changed":"content_invalid",
            "operation_incomplete":"content_invalid","operation_usage_invalid":"content_invalid",
            "operation_baseline_changed":"catalog_invalid","operation_workspace":"permission_denied",
            "pending_sample_pr":"pending_sample_pr","first20_complete":"first20_complete",
            "ambiguous_selection_plan":"ambiguous_plan","selection_plan_required":"catalog_invalid",
            "noncontiguous_catalog":"catalog_invalid","baseline_catalog_missing":"catalog_invalid","publication_policy_invalid":"content_invalid",
            "readonly_required":"readonly_required","sql_timeout_required":"sql_timeout_required",
            "candidate_plan_invalid":"selection_plan","candidate_plan_expensive":"selection_plan",
            "candidate_plan_unbounded":"selection_plan","candidate_index_required":"selection_plan",
            "no_eligible_matches_in_bounded_pool":"selection_empty",
            "candidate_schema":"selection_invalid","candidate_order":"selection_invalid",
            "candidate_page_limit":"selection_invalid","existing_root_review_required":"root_policy",
            "selected_scope_changed":"scope_changed","sql_row_limit":"row_limit",
            "sql_byte_limit":"byte_limit","candidate_row_limit":"row_limit",
            "manifest_invalid":"manifest_invalid","manifest_selection_mismatch":"manifest_invalid",
            "object_key_invalid":"manifest_invalid","version_selection_required":"manifest_invalid",
            "object_changed":"object_changed","s3_head_failed":"s3_head_failed",
            "sensitive_content":"sensitive_content","output_limit":"output_limit",
            "delivery_limit":"output_limit","decode_limit":"decode_limit","invalid_parquet":"decode_failed",
            "stage_capacity":"stage_capacity","stage_expired":"stage_expired",
            "stage_changed":"stage_changed","stage_review_changed":"stage_changed",
            "private_stage_required":"stage_private","stage_source_sha":"stage_changed",
            "publication_review_required":"review_required","stage_digest":"digest_mismatch",
            "review_digest_mismatch":"digest_mismatch","stage_response":"malformed_output",
            "ssh_output_limit":"output_limit","python_version":"python_version",
            "dependency_version":"dependency_version","config_missing":"config_missing",
            "config_limit":"config_invalid","config_invalid":"config_invalid",
            "config_duplicate":"config_invalid","config_ambiguous":"config_invalid",
            "config_endpoint":"config_invalid","config_bucket":"config_invalid",
            "deadline":"timeout","read_budget":"byte_limit","object_limit":"byte_limit",
        }
        if code in fixed: return fixed[code]
        state=getattr(exc,"pgcode",None)
        if state in ("42P01","42703","42883","42804"): return "schema_missing"
        if state=="42501": return "permission_denied"
        if state=="57014": return "query_timeout"
        if state=="55P03": return "lock_timeout"
        response=getattr(exc,"response",{})
        http=response.get("ResponseMetadata",{}).get("HTTPStatusCode")
        if http in (401,403): return "permission_denied"
        if http==404: return "object_missing"
        if http==412: return "object_changed"
        if isinstance(exc,ModuleNotFoundError): return "dependency_missing"
        if isinstance(exc,ImportError): return "imports_failed"
        if type(exc).__name__=="PackageNotFoundError": return "dependency_missing"
        if isinstance(exc,PermissionError): return "permission_denied"
        if isinstance(exc,FileNotFoundError):
            return "config_missing" if stage=="remote_config" else "project_unavailable"
        if isinstance(exc,TimeoutError) or type(exc).__name__ in ("ReadTimeoutError","ConnectTimeoutError"):
            return "timeout"
        if stage=="postgres_connect": return "connection_failed"
        if stage=="runner_request": return "invalid_input"
        if stage=="runner_capture": return "malformed_output"
        if stage=="postgres_selection": return "selection_invalid"
        if stage=="source_manifest": return "manifest_invalid"
        if stage=="parquet_decode": return "decode_failed"
        if stage=="remote_config": return "config_invalid"
        if stage=="s3_head": return "s3_head_failed"
        if stage=="s3_get": return "s3_get_failed"
    except Exception:
        pass
    return "unexpected"


@contextmanager
def at(stage):
    emit(stage,"start")
    try:
        yield
    except Exception as exc:
        if not reported(exc):
            emit(stage,"error",reason(stage,exc))
            try: exc._sample_diagnostic_reported=True
            except Exception: pass
        raise
    else:
        emit(stage,"ok")


def relay(source, output, limit=65536):
    """Accept only exact bounded protocol lines; discard every other stderr byte."""
    total=0
    while True:
        raw=source.readline(513)
        if not raw: break
        total+=len(raw)
        if total>limit:
            emit("diagnostic_channel","error","output_limit",output)
            break
        if not raw.endswith(b"\n"):
            while raw and not raw.endswith(b"\n") and total<=limit:
                raw=source.readline(513); total+=len(raw)
            if total>limit:
                emit("diagnostic_channel","error","output_limit",output)
                break
            continue
        try: line=raw.decode("ascii").rstrip("\n")
        except UnicodeError: continue
        parts=line.split(":")
        if len(parts)==4 and parts[0]=="sample_diag" and parts[1] in STAGES and parts[2] in STATUSES and parts[3] in REASONS:
            emit(parts[1],parts[2],parts[3],output)
        elif "Permission denied (publickey" in line:
            emit("ssh_transport","error","auth_denied",output)
        elif "Could not resolve hostname " in line:
            emit("ssh_transport","error","dns_failed",output)
        elif "Host key verification failed" in line:
            emit("ssh_transport","error","host_key_failed",output)
        elif "Connection refused" in line or "No route to host" in line:
            emit("ssh_transport","error","connect_failed",output)
        elif "Load key " in line and ("invalid format" in line or "error in libcrypto" in line):
            emit("ssh_transport","error","key_invalid",output)


if __name__=="__main__":
    relay(sys.stdin.buffer,sys.stderr)
