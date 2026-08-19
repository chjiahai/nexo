#!/usr/bin/env bash
  # 在云端 NATS 集群创建/更新 JetStream 流 WECOM_MSG。
  # 用法: ./create_stream.sh [nats-url]   默认 nats://10.13.11.7:4222
  # 幂等:流已存在则 update(只改可变字段),不存在则 create。
  set -euo pipefail

  STREAM="WECOM_MSG"
  SUBJECTS="nexo.wecom.msg.>"
  STORAGE="file"
  REPLICAS="3"
  RETENTION="limits"
  MAX_AGE="720h"
  DUPE_WINDOW="2m"
  MAX_MSGS="-1"      # 无上限(靠 max-age 淘汰)
  DISCARD="old"      # 设上限时淘汰旧、不丢新

  NATS_URL="${1:-${NATS_URL:-nats://10.13.11.7:4222}}"

  if ! command -v nats >/dev/null 2>&1; then
    echo "✗ 未找到 nats CLI。运行 scripts/nats/install.sh,或:" >&2
    echo "  curl -sf https://binaries.nats.dev/nats-io/nats/v2@latest/install | sh" >&2
    exit 1
  fi

  # create 用全集;update 去掉 storage(不可变)和 replicas(改会触发 resync,已正确无需动)。
  # 注意:`stream update` 不接受 `--defaults`(那是 create 的 flag),用 `--force` 跳过交互确认。
  CREATE_FLAGS=(
    --subjects="$SUBJECTS" --storage="$STORAGE" --replicas="$REPLICAS"
    --retention="$RETENTION" --max-age="$MAX_AGE" --dupe-window="$DUPE_WINDOW"
    --max-msgs="$MAX_MSGS" --discard="$DISCARD"
  )
  UPDATE_FLAGS=(
    --subjects="$SUBJECTS" --retention="$RETENTION"
    --max-age="$MAX_AGE" --dupe-window="$DUPE_WINDOW"
    --max-msgs="$MAX_MSGS" --discard="$DISCARD"
  )

  echo "→ NATS_URL = $NATS_URL"
  echo "→ $STREAM  subjects=$SUBJECTS  retention=$RETENTION  max-age=$MAX_AGE  dupe-window=$DUPE_WINDOW  max-msgs=$MAX_MSGS  discard=$DISCARD"

  # 流级 JS API 走默认 account,无需 $SYS 系统权限;连任一 core 节点即可操作整个集群。
  if nats --server="$NATS_URL" stream info "$STREAM" >/dev/null 2>&1; then
    echo "→ 流已存在,执行 update(可变字段)..."
    nats --server="$NATS_URL" stream update "$STREAM" --force "${UPDATE_FLAGS[@]}"
  else
    echo "→ 流不存在,执行 create..."
    nats --server="$NATS_URL" stream create "$STREAM" --defaults "${CREATE_FLAGS[@]}"
  fi

  echo
  echo "✓ 完成,流现状:"
  nats --server="$NATS_URL" stream info "$STREAM"