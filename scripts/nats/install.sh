#!/usr/bin/env bash
# scripts/nats/install.sh — install nats-server + nats CLI + config + (Linux) systemd autostart.
#
# Usage:
#   bash scripts/nats/install.sh <role>
#     role ∈ core-a | core-b | core-c | leaf
#
# - core-a/b/c: the 3 cloud hosts (10.13.11.7 / .17 / .177) — JetStream + cluster.
# - leaf:       the 2 Linux desktops + the Macbook — leaf node, no JetStream.
#
# Linux:  binary -> /usr/local/bin, config -> /etc/nats/nats-server.conf,
#         systemd unit enabled + started (needs root / sudo).
# macOS:  binary -> /usr/local/bin (arm64: /opt/homebrew/bin),
#         config -> /usr/local/etc/nats/nats-server.conf. NO service — start
#         manually (laptops sleep often; autostart adds little). The script
#         prints the start command at the end.
#
# Idempotent: re-running overwrites the config and restarts the service.
#
# Env overrides:
#   NATS_REF  version selector passed to the official installer (default: latest)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NATS_REF="${NATS_REF:-latest}"

# --- systemd unit (Linux) ---------------------------------------------------
# nats-server listens on the WireGuard IP; if WG isn't up yet at start time the
# bind fails and Restart=on-failure retries every 5s until it is. If you manage
# WG with wg-quick, add `wg-quick@<if>.service` to After=/Wants= below.
read -r -d '' UNIT <<'EOF' || true
[Unit]
Description=NATS Server (nexo)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nats
Group=nats
ExecStart=/usr/local/bin/nats-server -c /etc/nats/nats-server.conf
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

install_linux() {
  echo "==> installing binary -> /usr/local/bin/nats-server"
  $SUDO install -m 0755 "$BIN_SRC" /usr/local/bin/nats-server

  echo "==> installing config -> /etc/nats/nats-server.conf"
  $SUDO mkdir -p /etc/nats
  $SUDO install -m 0644 "$CONF_SRC" /etc/nats/nats-server.conf

  # Dedicated unprivileged user — the systemd unit always runs as `nats`,
  # so create it on every Linux node (core AND leaf).
  if ! id nats >/dev/null 2>&1; then
    $SUDO useradd --system --no-create-home --shell /usr/sbin/nologin nats
  fi

  if [[ "$IS_CORE" == "yes" ]]; then
    # JetStream data dir (core nodes only; leaf has no JetStream).
    $SUDO mkdir -p /var/lib/nats/jetstream
    $SUDO chown -R nats:nats /var/lib/nats
  fi

  echo "==> writing systemd unit -> /etc/systemd/system/nats.service"
  echo "$UNIT" | $SUDO tee /etc/systemd/system/nats.service >/dev/null
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now nats.service

  echo "==> nats.service enabled + started"
  echo "    status:   systemctl status nats"
  echo "    logs:     journalctl -u nats -f"
  if [[ "$IS_CORE" == "yes" ]]; then
    echo "    data dir: /var/lib/nats/jetstream (retained on uninstall)"
  fi
}

install_macos() {
  # arm64 Macs use /opt/homebrew/bin; Intel uses /usr/local/bin.
  if [[ "$NATS_ARCH" == "arm64" ]] && [[ -d /opt/homebrew/bin ]]; then
    BIN_DIR="/opt/homebrew/bin"
  else
    BIN_DIR="/usr/local/bin"
  fi
  CONF_DIR="/usr/local/etc/nats"

  echo "==> installing binary -> ${BIN_DIR}/nats-server"
  mkdir -p "$BIN_DIR"
  install -m 0755 "$BIN_SRC" "${BIN_DIR}/nats-server"

  # Apple Silicon kills unsigned arm64 binaries with SIGKILL; ad-hoc sign so it
  # runs. Harmless on Intel macOS; Linux doesn't go through this branch.
  if command -v codesign >/dev/null 2>&1; then
    codesign --sign - "${BIN_DIR}/nats-server" 2>/dev/null || true
  fi

  echo "==> installing config -> ${CONF_DIR}/nats-server.conf"
  mkdir -p "$CONF_DIR"
  install -m 0644 "$CONF_SRC" "${CONF_DIR}/nats-server.conf"

  cat <<EOF
==> done. No autostart on macOS — start manually:
    nats-server -c ${CONF_DIR}/nats-server.conf
EOF
}

# --- nats CLI (natscli) -----------------------------------------------------
# macOS:  prefer the Homebrew tap (properly signed bottle — arm64 safe); fall
#         back to the official binary installer if brew is missing OR the tap
#         clone fails (e.g. github.com unreachable).
# Linux:  official binary installer -> /usr/local/bin/nats.
# The CLI is independent of role; installed on every node so `nats stream info`
# etc. work locally. Env override NATS_REF pins the version (default: latest).
install_cli() {
  echo "==> installing nats CLI (natscli)"

  if [[ "$OS" == "Darwin" ]]; then
    if command -v brew >/dev/null 2>&1 && brew install nats-io/nats-tools/nats; then
      return
    fi
    echo "    (brew unavailable or tap failed; using the official binary installer)"
  fi

  ( cd "$TMP_DIR" && curl -sf "https://binaries.nats.dev/nats-io/natscli/nats@${NATS_REF}" | sh )
  CLI_SRC="${TMP_DIR}/nats"
  if [[ ! -f "$CLI_SRC" ]]; then
    echo "ERROR: nats CLI installer did not produce 'nats' in ${TMP_DIR}" >&2
    exit 1
  fi

  if [[ "$OS" == "Darwin" ]]; then
    install -m 0755 "$CLI_SRC" "${BIN_DIR}/nats"
    # Apple Silicon SIGKILLs unsigned arm64 binaries (same as nats-server).
    command -v codesign >/dev/null 2>&1 && codesign --sign - "${BIN_DIR}/nats" 2>/dev/null || true
  else
    $SUDO install -m 0755 "$CLI_SRC" /usr/local/bin/nats
  fi
}

# --- parse role -------------------------------------------------------------
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <role: core-a|core-b|core-c|leaf>" >&2
  exit 1
fi
ROLE="$1"

if [[ "$ROLE" == "leaf" ]]; then
  CONF_SRC="${SCRIPT_DIR}/leaf.conf"
elif [[ "$ROLE" == core-a || "$ROLE" == core-b || "$ROLE" == core-c ]]; then
  CONF_SRC="${SCRIPT_DIR}/${ROLE}.conf"
else
  echo "ERROR: unknown role '$ROLE' (expected core-a|core-b|core-c|leaf)" >&2
  exit 1
fi
if [[ ! -f "$CONF_SRC" ]]; then
  echo "ERROR: config not found: $CONF_SRC" >&2
  exit 1
fi

# --- OS / arch detection ----------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Linux)  NATS_OS="linux" ;;
  Darwin) NATS_OS="darwin" ;;
  *) echo "ERROR: unsupported OS '$OS' (only Linux/macOS)" >&2; exit 1 ;;
esac
case "$ARCH" in
  x86_64|amd64)  NATS_ARCH="amd64" ;;
  aarch64|arm64) NATS_ARCH="arm64" ;;
  *) echo "ERROR: unsupported arch '$ARCH'" >&2; exit 1 ;;
esac

IS_CORE="no"
[[ "$ROLE" == core-* ]] && IS_CORE="yes"

# root/sudo for Linux writes under /usr/local, /etc, /var/lib, systemd.
SUDO=""
if [[ "$OS" == "Linux" && "$(id -u)" -ne 0 ]]; then
  SUDO="sudo"
fi

echo "==> role=${ROLE} os=${NATS_OS}/${NATS_ARCH} ref=${NATS_REF}"

# --- download via the official installer ------------------------------------
# https://docs.nats.io/running-a-nats-service/introduction/installation
# `curl ... | sh` writes `nats-server` into the current working directory; the
# installer detects OS/arch itself. Run it in a temp dir so we don't litter CWD.
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "==> downloading nats-server (v2@${NATS_REF}) via official installer"
( cd "$TMP_DIR" && curl -sf "https://binaries.nats.dev/nats-io/nats-server/v2@${NATS_REF}" | sh )
BIN_SRC="${TMP_DIR}/nats-server"
if [[ ! -f "$BIN_SRC" ]]; then
  echo "ERROR: installer did not produce nats-server in ${TMP_DIR}" >&2
  exit 1
fi

# --- install per-OS ---------------------------------------------------------
if [[ "$OS" == "Linux" ]]; then
  install_linux
else
  install_macos
fi

install_cli
