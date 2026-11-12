#!/usr/bin/env bash
# Manual production cutover helper. Never run unattended.
set -Eeuo pipefail
umask 077
ROOT=/root/Tennis_pelmen_model
[[ "${EXPECTED_OLD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid expected old SHA"; exit 2; }
[[ "${TARGET_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid target SHA"; exit 2; }
cd "$ROOT"
[[ "$(git rev-parse --verify HEAD)" == "$EXPECTED_OLD_SHA" ]] || { echo "Unexpected deployed SHA"; exit 2; }
[[ -z "$(git status --porcelain --untracked-files=normal)" ]] || { echo "Dirty worktree"; exit 2; }
[[ -x .venv/bin/python ]] || { echo "Missing venv"; exit 2; }
# Prevent new cron detail jobs and wait for running ones to finish.
exec 8>/tmp/nbbet_details.lock
exec 9>/tmp/nbbet_refresh.lock
flock -w 300 8 || { echo "Details lock busy"; exit 2; }
flock -w 300 9 || { echo "Refresh lock busy"; exit 2; }
[[ "$(git rev-parse --verify HEAD)" == "$EXPECTED_OLD_SHA" ]] || exit 2
[[ -z "$(git status --porcelain --untracked-files=normal)" ]] || exit 2
# Fetch only the main ref; do not reset or clean, and do not alter DB/S3/cron.
git fetch --no-tags origin main
[[ "$(git rev-parse FETCH_HEAD)" == "$TARGET_SHA" ]] || { echo "Target is not fetched main HEAD"; exit 2; }
# Reuse existing venv; this is safe only if dependency manifests are unchanged.
git diff --quiet "$EXPECTED_OLD_SHA" "$TARGET_SHA" -- pyproject.toml uv.lock || { echo "Dependencies changed: stop"; exit 2; }
rollback() {
  code=$?
  trap - ERR
  if [[ "$code" -ne 0 ]]; then
    echo "Cutover failed, attempting code rollback"
    git checkout --detach "$EXPECTED_OLD_SHA" || echo "CRITICAL: manual rollback needed"
  fi
  exit "$code"
}
trap rollback ERR
git checkout --detach "$TARGET_SHA"
# Explicitly verify the interpreter resolves imports to the checked-out source.
.venv/bin/python -B - <<'PY'
import pathlib
import tennis_predictor.ingestion.nbbet.detail_job as module
root = pathlib.Path('/root/Tennis_pelmen_model/src').resolve()
actual = pathlib.Path(module.__file__).resolve()
assert actual.is_relative_to(root), f"wrong module path: {actual}"
print("detail_job_import=PASS")
PY
[[ "$(git rev-parse --verify HEAD)" == "$TARGET_SHA" ]] || false
trap - ERR
echo "CUTOVER_OK: target=$TARGET_SHA previous=$EXPECTED_OLD_SHA"
echo "Locks release on exit. This script does NOT automatically roll back later ingestion regressions."
echo "Mandatory: observe at least one 10-minute cycle and manually initiate rollback if metrics regress."
