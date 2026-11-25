#!/usr/bin/env bash
# Reuse existing credentials and fingerprint; never send a GitHub write token.
set -euo pipefail
umask 077
fail() { echo "sample_ssh_failed" >&2; exit 1; }
[[ "$SSH_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || fail
[[ "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ && -n "$SSH_KEY" ]] || fail
[[ "$SSH_USER" != root || "$ALLOW_EXISTING_ROOT" == true ]] || fail
[[ "$SSH_FINGERPRINT" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || fail
[[ "$PROJECT_PATH" =~ ^/[A-Za-z0-9_./-]{1,200}$ ]] || fail
work=$(mktemp -d "$RUNNER_TEMP/sample-ssh.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
# Build validates all inputs, including path segments and review attestations, first.
python scripts/sample_workflow_request.py > "$work/request.py" 2>/dev/null || fail
timeout 15s ssh-keyscan -T 10 -p 22 -t ed25519 -- "$SSH_HOST" > "$work/candidate" 2>/dev/null || fail
awk 'NF == 3 && $2 == "ssh-ed25519" { print; count++ }
     END { if (count != 1) exit 1 }' "$work/candidate" > "$work/known_hosts" || fail
actual=$(ssh-keygen -lf "$work/known_hosts" -E sha256 2>/dev/null | awk '{print $2}')
[[ "$actual" == "$SSH_FINGERPRINT" ]] || fail
printf '%s\n' "$SSH_KEY" > "$work/key"
# All output goes to bounded private files via the capture helper, never Actions logs.
remote="cd '$PROJECT_PATH' && timeout --kill-after=5s 260s .venv/bin/python -I -B -"
unset GH_TOKEN GITHUB_TOKEN
timeout --kill-after=5s 290s ssh -F /dev/null -T -p 22 \
  -o BatchMode=yes -o ConnectionAttempts=1 -o ConnectTimeout=15 \
  -o HostKeyAlgorithms=ssh-ed25519 -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$work/known_hosts" -o GlobalKnownHostsFile=/dev/null \
  -o UpdateHostKeys=no -o VerifyHostKeyDNS=no -o IdentitiesOnly=yes \
  -o IdentityAgent=none -o PreferredAuthentications=publickey \
  -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
  -o ForwardAgent=no -o ClearAllForwardings=yes -i "$work/key" \
  -l "$SSH_USER" -- "$SSH_HOST" "$remote" < "$work/request.py" 2>/dev/null \
  | python scripts/sample_workflow_capture.py 2>/dev/null || fail
