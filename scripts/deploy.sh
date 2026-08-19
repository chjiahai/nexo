#!/usr/bin/env bash
# scripts/deploy.sh [profile] — rsync code to a host and rebuild its services.
#
# profile (default: bot) selects scripts/deploy.<profile>.env, which defines
# REMOTE_HOST / REMOTE_USER / REMOTE_PATH / SERVICES for that target:
#   - bot     → 10.13.11.1   (core-b, Tencent — SSH port 63208): `nexo bot` + `nexo drain`
#   - archive → 10.13.11.177 (core-c): `nexo archive`
#
# The remotes have no GitHub access, so code is shipped via rsync from the local
# working tree. Host-local files (.env, docker-compose.override.yml) are
# excluded — configured once on the remote, preserved across deploys.
#
# Usage:
#   bash scripts/deploy.sh            # default profile (bot)
#   bash scripts/deploy.sh archive
#
# Prereqs (one-time per profile):
#   1. cp scripts/deploy.env.example scripts/deploy.<profile>.env && fill in.
#   2. ssh-copy-id <REMOTE_USER>@<REMOTE_HOST>   (so SSH is passwordless)
#   3. On the remote: .env configured, data/ writable by uid 10001 (bot/drain),
#      docker-compose.override.yml present (PyPI/apt mirror build-args).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROFILE="${1:-bot}"
ENV_FILE="${SCRIPT_DIR}/deploy.${PROFILE}.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: ${ENV_FILE} not found." >&2
  echo "  cp scripts/deploy.env.example scripts/deploy.${PROFILE}.env  && fill in REMOTE_* + SERVICES" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "${ENV_FILE}"

: "${REMOTE_HOST:?REMOTE_HOST not set in ${ENV_FILE}}"
: "${REMOTE_USER:?REMOTE_USER not set in ${ENV_FILE}}"
: "${REMOTE_PATH:?REMOTE_PATH not set in ${ENV_FILE}}"
: "${SERVICES:?SERVICES not set in ${ENV_FILE} (e.g. \"nexo drain\" or \"archive\")}"

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"
# SSH port — defaults to 22; the Tencent core-b host (.1) uses 63208.
SSH_PORT="${REMOTE_PORT:-22}"

echo "==> profile=${PROFILE}  target=${REMOTE}:${SSH_PORT}:${REMOTE_PATH}  services=\"${SERVICES}\""

# 1. Fail fast if SSH key auth isn't set up — don't hang on a password prompt.
if ! ssh -p "${SSH_PORT}" -o BatchMode=yes -o ConnectTimeout=5 "${REMOTE}" true 2>/dev/null; then
  echo "ERROR: passwordless SSH to ${REMOTE}:${SSH_PORT} failed." >&2
  echo "  Run: ssh-copy-id -p ${SSH_PORT} ${REMOTE}" >&2
  exit 1
fi

# 2. Warn (don't fail) if the working tree is dirty — rsync would ship
#    uncommitted changes, which may not match origin/main.
if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null)" ]]; then
  echo "WARNING: working tree has uncommitted changes — rsync will ship them as-is." >&2
fi

# 3. rsync the working tree. --delete keeps the remote tree clean; the excluded
#    host-local files (.env, docker-compose.override.yml, deploy.*.env) are
#    preserved on the remote (rsync --delete does not remove excluded files).
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
  --exclude='scripts/deploy.*.env' \
  --exclude='docker-compose.override.yml' \
  -e "ssh -p ${SSH_PORT}" \
  "${REPO_ROOT}/" "${REMOTE}:${REMOTE_PATH}/"

# 4. Warn (don't fail) if .env is missing — compose env_file needs it.
if ! ssh -p "${SSH_PORT}" "${REMOTE}" "test -f ${REMOTE_PATH}/.env"; then
  echo "WARNING: ${REMOTE_PATH}/.env missing on remote — docker compose will fail to start." >&2
fi

# 5. Remote: rebuild + restart the profile's services.
echo "==> docker compose up -d --build ${SERVICES} (rebuilds the nexo image)..."
ssh -p "${SSH_PORT}" "${REMOTE}" bash -lc "'
  set -e
  cd ${REMOTE_PATH}
  docker compose up -d --build ${SERVICES}
'"

# 6. Show status + recent logs for the deployed services.
echo
echo "==> status:"
ssh -p "${SSH_PORT}" "${REMOTE}" bash -lc "'
  cd ${REMOTE_PATH}
  docker compose ps
  for svc in ${SERVICES}; do
    echo --- recent \$svc logs ---
    docker compose logs --tail=15 \$svc
  done
'"

echo "==> deploy complete."
