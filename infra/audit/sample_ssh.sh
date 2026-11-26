#!/usr/bin/env bash
# Fixed stage/reason diagnostics only; no raw stderr, source data or credentials.
set -euo pipefail
umask 077
diag() { printf 'sample_diag:%s:%s:%s\n' "$1" "$2" "${3:-none}" >&2; }
fail() { diag "$1" error "$2"; exit 1; }
diag runner_preflight start
for tool in python timeout ssh ssh-keyscan ssh-keygen awk mkfifo mktemp; do
  command -v "$tool" >/dev/null 2>&1 || fail runner_preflight tool_missing
done
python -I -S -c 'import sys; sys.exit(sys.version_info < (3,11))' >/dev/null 2>&1 \
  || fail runner_preflight python_version
[[ -n "${SSH_HOST:-}" && -n "${SSH_USER:-}" && -n "${SSH_KEY:-}" ]] \
  || fail runner_preflight ssh_settings_missing
[[ "$SSH_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ && "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] \
  || fail runner_preflight invalid_input
[[ "$SSH_USER" != root || "${ALLOW_EXISTING_ROOT:-false}" == true ]] \
  || fail runner_preflight root_policy
[[ "${SSH_FINGERPRINT:-}" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] \
  || fail runner_preflight fingerprint_invalid
[[ "${PROJECT_PATH:-}" =~ ^/[A-Za-z0-9_./-]{1,200}$ ]] \
  || fail runner_preflight project_invalid
[[ -n "${RUNNER_TEMP:-}" ]] || fail runner_preflight invalid_input
work=$(mktemp -d "$RUNNER_TEMP/sample-ssh.XXXXXX" 2>/dev/null) || fail runner_preflight project_unavailable
relay_pid=""
trap 'if [[ -n "$relay_pid" ]]; then kill "$relay_pid" 2>/dev/null || true; wait "$relay_pid" 2>/dev/null || true; fi; rm -rf -- "$work" 2>/dev/null || true' EXIT
diag runner_preflight ok
if ! python -I -S scripts/sample_workflow_request.py > "$work/request.py" 2> "$work/request.err"; then
  python -I -S infra/audit/sample_diagnostics.py < "$work/request.err"
  fail runner_request request_failed
fi
python -I -S infra/audit/sample_diagnostics.py < "$work/request.err"
diag host_scan start
set +e
timeout 15s ssh-keyscan -T 10 -p 22 -t ed25519 -- "$SSH_HOST" > "$work/candidate" 2>/dev/null
scan_rc=$?
set -e
if [[ "$scan_rc" == 124 ]]; then fail host_scan timeout; fi
[[ "$scan_rc" == 0 ]] || fail host_scan discovery_failed
diag host_scan ok
diag host_verify start
awk 'NF == 3 && $2 == "ssh-ed25519" { print; count++ }
     END { if (count != 1) exit 1 }' "$work/candidate" > "$work/known_hosts" 2>/dev/null \
  || fail host_verify key_count
actual=$(ssh-keygen -lf "$work/known_hosts" -E sha256 2>/dev/null | awk '{print $2}') \
  || fail host_verify key_format
[[ "$actual" == "$SSH_FINGERPRINT" ]] || fail host_verify fingerprint_mismatch
{ printf '%s\n' "$SSH_KEY" > "$work/key"; } 2>/dev/null || fail host_verify unexpected
diag host_verify ok
remote="printf 'sample_diag:remote_entry:ok:none\n' >&2; \
if ! cd '$PROJECT_PATH' 2>/dev/null; then printf 'sample_diag:remote_runtime:error:project_unavailable\n' >&2; exit 1; fi; \
if ! test -x .venv/bin/python; then printf 'sample_diag:remote_runtime:error:python_unavailable\n' >&2; exit 1; fi; \
if ! command -v timeout >/dev/null 2>&1; then printf 'sample_diag:remote_runtime:error:timeout_missing\n' >&2; exit 1; fi; \
printf 'sample_diag:remote_runtime:ok:none\n' >&2; \
timeout --kill-after=5s 260s .venv/bin/python -I -B -"
unset GH_TOKEN GITHUB_TOKEN
mkfifo "$work/remote.err" 2>/dev/null || fail ssh_transport unexpected
python -I -S infra/audit/sample_diagnostics.py < "$work/remote.err" >&2 &
relay_pid=$!
diag ssh_transport start
summary="${GITHUB_STEP_SUMMARY:-}"
set +e
timeout --kill-after=5s 290s ssh -F /dev/null -T -p 22 \
  -o BatchMode=yes -o ConnectionAttempts=1 -o ConnectTimeout=15 \
  -o HostKeyAlgorithms=ssh-ed25519 -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$work/known_hosts" -o GlobalKnownHostsFile=/dev/null \
  -o UpdateHostKeys=no -o VerifyHostKeyDNS=no -o IdentitiesOnly=yes \
  -o IdentityAgent=none -o PreferredAuthentications=publickey \
  -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
  -o ForwardAgent=no -o ClearAllForwardings=yes -i "$work/key" \
  -l "$SSH_USER" -- "$SSH_HOST" "$remote" < "$work/request.py" 2> "$work/remote.err" \
  | GITHUB_STEP_SUMMARY="$work/summary" python -I -S scripts/sample_workflow_capture.py 2> "$work/capture.err"
codes=("${PIPESTATUS[@]}")
wait "$relay_pid"
relay_rc=$?
relay_pid=""
set -e
if [[ "${codes[0]}" != 0 ]]; then
  diag runner_capture skipped upstream_failed
  if [[ "${codes[0]}" == 124 || "${codes[0]}" == 137 ]]; then fail ssh_transport timeout; fi
  if [[ "${codes[0]}" == 255 ]]; then fail ssh_transport auth_or_transport_failed; fi
  fail ssh_transport remote_exit
fi
diag ssh_transport ok
python -I -S infra/audit/sample_diagnostics.py < "$work/capture.err"
[[ "$relay_rc" == 0 ]] || fail diagnostic_channel unexpected
[[ "${codes[1]}" == 0 ]] || fail runner_capture capture_failed
if [[ -f "$work/summary" && -n "$summary" ]]; then
  { cat "$work/summary" >> "$summary"; } 2>/dev/null || fail runner_capture unexpected
fi
