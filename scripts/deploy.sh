#!/usr/bin/env bash
# scripts/deploy.sh — rsync code to the remote host and rebuild bot + drain.
#
# The remote (10.13.11.17 = core-b NATS node) has no GitHub access, so code is
# shipped via rsync from the local working tree (not `git pull`). Host-local
# files (.env, docker-compose.override.yml) are excluded — they're configured
# once on the remote and must survive deploys.
#
# Only `nexo` (bot) + `drain` containers are started. The compose `nats` service
# is NOT started — the host runs core-b nats as a systemd service on :4222.
# (The Langfuse stack runs separately at 10.13.11.7:3000.)
#
# rsync ships the LOCAL working tree as-is, so commit (and push) first if you
# want the deploy to match origin/main; the script warns if the tree is dirty.
#
# Run manually: `bash scripts/deploy.sh`.
# (Pushing code does not deploy — deploy is always a deliberate, manual step.)
#
# Prereqs (one-time):
#   1. cp scripts/deploy.env.example scripts/deploy.env && fill in REMOTE_*.
#   2. ssh-copy-id <REMOTE_USER>@<REMOTE_HOST>   (so SSH is passwordless)
#   3. On the remote: .env configured (NATS_URL → the host's core node),
#      data/ writable by uid 10001, and — if using a PyPI mirror —
#      docker-compose.override.yml present (it carries the build-args).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
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

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

echo "==> target: ${REMOTE}:${REMOTE_PATH}"

# 1. Fail fast if SSH key auth isn't set up — don't hang on a password prompt.
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "${REMOTE}" true 2>/dev/null; then
  echo "ERROR: passwordless SSH to ${REMOTE} failed." >&2
  echo "  Run: ssh-copy-id ${REMOTE}" >&2
  exit 1
fi

# 2. Warn (don't fail) if the working tree is dirty — rsync would ship
#    uncommitted changes, which may not match origin/main.
if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null)" ]]; then
  echo "WARNING: working tree has uncommitted changes — rsync will ship them as-is." >&2
fi

# 3. rsync the working tree. --delete keeps the remote tree clean; the excluded
#    host-local files (.env, docker-compose.override.yml) are preserved on the
#    remote (rsync --delete does not remove excluded destination files).
echo "==> rsyncing code to ${REMOTE}:${REMOTE_PATH}..."
rsync -az --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='data' \
  --exclude='inbox' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='.DS_Store' \
  --exclude='.vscode' \
  --exclude='.claude' \
  --exclude='notebooks' \
  --exclude='.env' \
  --exclude='scripts/deploy.env' \
  --exclude='docker-compose.override.yml' \
  -e ssh \
  "${REPO_ROOT}/" "${REMOTE}:${REMOTE_PATH}/"

# 4. Warn (don't fail) if .env is missing — compose env_file needs it.
if ! ssh "${REMOTE}" "test -f ${REMOTE_PATH}/.env"; then
  echo "WARNING: ${REMOTE_PATH}/.env missing on remote — docker compose will fail to start nexo." >&2
fi

# 5. Remote: rebuild + restart bot + drain (NOT the nats service).
echo "==> docker compose up -d --build nexo drain (rebuilds the nexo image)..."
ssh "${REMOTE}" bash -lc "'
  set -e
  cd ${REMOTE_PATH}
  docker compose up -d --build nexo drain
'"

# 6. Show status + recent logs so the operator can see it's live.
echo
echo "==> status:"
ssh "${REMOTE}" bash -lc "'
  cd ${REMOTE_PATH}
  docker compose ps
  echo --- recent nexo logs ---
  docker compose logs --tail=20 nexo
  echo --- recent drain logs ---
  docker compose logs --tail=10 drain
'"

echo "==> deploy complete."
