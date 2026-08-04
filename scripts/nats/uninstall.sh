#!/usr/bin/env bash
# scripts/nats/uninstall.sh — remove nats-server, its config, and the systemd
# unit. KEEPS the JetStream data dir (/var/lib/nats) and the `nats` user so
# core-node messages survive a reinstall.
#
# Usage:
#   bash scripts/nats/uninstall.sh
#
# Run on any node (Linux or macOS). Safe to run on a partially-installed host.
# To also delete retained JetStream data, remove /var/lib/nats manually.

set -euo pipefail

OS="$(uname -s)"
SUDO=""
if [[ "$OS" == "Linux" && "$(id -u)" -ne 0 ]]; then
  SUDO="sudo"
fi

if [[ "$OS" == "Linux" ]]; then
  echo "==> stopping + disabling nats.service (if present)"
  $SUDO systemctl stop nats.service 2>/dev/null || true
  $SUDO systemctl disable nats.service 2>/dev/null || true

  echo "==> removing unit / binary / config"
  $SUDO rm -f /etc/systemd/system/nats.service
  $SUDO systemctl daemon-reload 2>/dev/null || true
  $SUDO rm -f /usr/local/bin/nats-server
  $SUDO rm -f /etc/nats/nats-server.conf
  $SUDO rmdir /etc/nats 2>/dev/null || true

  if [[ -d /var/lib/nats ]]; then
    echo "==> KEPT /var/lib/nats (JetStream data). To delete: sudo rm -rf /var/lib/nats"
  fi
else
  # macOS — no service.
  echo "==> removing binary / config"
  rm -f /usr/local/bin/nats-server /opt/homebrew/bin/nats-server
  rm -f /usr/local/etc/nats/nats-server.conf
  rmdir /usr/local/etc/nats 2>/dev/null || true
fi

echo "==> done."
