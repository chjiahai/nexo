#!/usr/bin/env bash
# scripts/deploy.sh — sync code to the remote test host and rebuild the service.
#
# Flow: SSH to remote -> git pull the branch -> docker compose up -d --build.
# The local machine has no docker, so the build always happens on the remote.
# Code reaches the remote via GitHub (the local `git push` already sent it
# there); this script only triggers the remote `git pull`.
#
# Triggered automatically by .git/hooks/post-push (when main is pushed), or run
# manually after a push: `bash scripts/deploy.sh`.
#
# Prereqs (one-time):
#   1. cp scripts/deploy.env.example scripts/deploy.env && fill it in.
#   2. ssh-copy-id <REMOTE_USER>@<REMOTE_HOST>   (so SSH is passwordless)
#   3. On the remote: .env configured, data/ writable by uid 10001.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/deploy.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: ${ENV_FILE} not found." >&2
  echo "  cp scripts/deploy.env.example scripts/deploy.env  && fill in REMOTE_*" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "${ENV_FILE}"

: "${REMOTE_HOST:?REMOTE_HOST not set in deploy.env}"
: "${REMOTE_USER:?REMOTE_USER not set in deploy.env}"
: "${REMOTE_PATH:?REMOTE_PATH not set in deploy.env}"
: "${REMOTE_BRANCH:=main}"

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

echo "==> target: ${REMOTE}:${REMOTE_PATH} (branch ${REMOTE_BRANCH})"

# 1. Fail fast if SSH key auth isn't set up — don't hang on a password prompt.
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "${REMOTE}" true 2>/dev/null; then
  echo "ERROR: passwordless SSH to ${REMOTE} failed." >&2
  echo "  Run: ssh-copy-id ${REMOTE}" >&2
  exit 1
fi

# 2. Remote: pull the branch (fast-forward only — refuse to silently merge
#    divergent history; surface it for manual resolution).
echo "==> pulling ${REMOTE_BRANCH} on remote..."
ssh "${REMOTE}" bash -lc "'
  set -e
  cd ${REMOTE_PATH}
  git fetch origin
  git checkout ${REMOTE_BRANCH}
  git pull --ff-only origin ${REMOTE_BRANCH}
'"

# 3. Warn (don't fail) if .env is missing — compose env_file needs it.
if ! ssh "${REMOTE}" "test -f ${REMOTE_PATH}/.env"; then
  echo "WARNING: ${REMOTE_PATH}/.env missing on remote — docker compose will fail to start nexo." >&2
fi

# 4. Remote: rebuild + restart. otel-lgtm comes up too via depends_on.
echo "==> docker compose up -d --build (this rebuilds the nexo image)..."
ssh "${REMOTE}" bash -lc "'
  cd ${REMOTE_PATH}
  docker compose up -d --build
'"

# 5. Show status + recent logs so the operator can see it's live.
echo
echo "==> status:"
ssh "${REMOTE}" bash -lc "'
  cd ${REMOTE_PATH}
  docker compose ps
  echo --- recent nexo logs ---
  docker compose logs --tail=20 nexo
'"

echo "==> deploy complete."
