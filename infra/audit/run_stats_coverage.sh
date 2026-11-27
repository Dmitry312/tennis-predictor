#!/usr/bin/env bash
# Execute tested public audit code without touching the production checkout.
set -euo pipefail
umask 077
audit_sha="${1:-}"
[[ "$audit_sha" =~ ^[a-f0-9]{40}$ ]] || { echo "tested_commit_sha_required" >&2; exit 1; }
audit_state=/var/tmp/tennis-stats-coverage-v1
mkdir -p -- "$audit_state"
[[ ! -L "$audit_state" ]] || exit 1
[[ "$(stat -c %u -- "$audit_state")" == "$(id -u)" && "$(stat -c %a -- "$audit_state")" == 700 ]] || exit 1
audit_code="$audit_state/code-$audit_sha"
mkdir -p -- "$audit_code"
[[ ! -L "$audit_code" ]] || exit 1
for audit_file in stats_coverage_core.py production_stats_coverage.py; do
  if [[ ! -f "$audit_code/$audit_file" ]]; then
    curl --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 30 \
      "https://raw.githubusercontent.com/Dmitry312/tennis-predictor/$audit_sha/infra/audit/$audit_file" \
      -o "$audit_code/$audit_file.tmp"
    mv -- "$audit_code/$audit_file.tmp" "$audit_code/$audit_file"
  fi
done
export OPENBLAS_NUM_THREADS=1
exec ionice -c 3 /root/Tennis_pelmen_model/.venv/bin/python -I -B \
  "$audit_code/production_stats_coverage.py" --state-dir "$audit_state"
