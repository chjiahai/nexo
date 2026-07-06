#!/usr/bin/env bash
# scripts/push.sh — push to origin AND deploy to the remote test host in one go.
#
# Git has NO client-side "post-push" hook (only pre-push, which fires before
# the push and would deploy stale code). So this wrapper is the deploy trigger:
# it runs `git push`, and only if that succeeds, runs scripts/deploy.sh.
#
# Usage:
#   bash scripts/push.sh              # git push (default) then deploy
#   bash scripts/push.sh origin main  # explicit remote + branch then deploy
#
# Prereqs: scripts/deploy.env filled in, SSH key copied to the remote
# (see scripts/deploy.env.example).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> git push $*"
git push "$@"

echo
echo "==> push succeeded; deploying to remote test host..."
bash "${SCRIPT_DIR}/deploy.sh"
