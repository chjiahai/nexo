#!/usr/bin/env bash
# ship_media.sh — 把一个已下载解密的媒体文件 scp 到远程机器的指定文件夹。
#
# 由 nexo.storage.remote 通过 subprocess 调用（不在 shell 里直接跑用户输入）。
# 用法: ship_media.sh <local_file> <remote_relative_path>
#   <local_file>           本地临时文件路径（调用方负责创建/清理）
#   <remote_relative_path> 远程目标相对路径，如 docs/2/20260728-120000-report.pdf
#                          （由 remote.py 构造，路径分量已清洗，无遍历/无冒号）
#
# 环境变量（必填）:
#   NEXO_UPLOAD_HOST  远程主机 IP/域名
#   NEXO_UPLOAD_USER  远程 SSH 登录用户
#   NEXO_UPLOAD_DIR   远程接收目录的绝对路径，如 /data/nexo-uploads
# 环境变量（可选）:
#   NEXO_UPLOAD_SSH_KEY   私钥路径（容器内），留空则用 ssh 默认/ssh-agent
#   NEXO_UPLOAD_SSH_PORT  SSH 端口，默认 22
#
# 失败以非零退出；调用方把非零退出视为瞬时错误并重试。
set -euo pipefail

: "${NEXO_UPLOAD_HOST:?NEXO_UPLOAD_HOST 未设置}"
: "${NEXO_UPLOAD_USER:?NEXO_UPLOAD_USER 未设置}"
: "${NEXO_UPLOAD_DIR:?NEXO_UPLOAD_DIR 未设置}"

local_file="$1"
rel_path="$2"

ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
# known_hosts 放 /tmp（容器内 appuser 可写）；挂载的私钥使 /home/app/.ssh 由 root
# 所有、不可写，故不使用默认 ~/.ssh/known_hosts。
ssh_opts+=(-o UserKnownHostsFile=/tmp/nexo_known_hosts)
[[ -n "${NEXO_UPLOAD_SSH_KEY:-}" ]] && ssh_opts+=(-i "${NEXO_UPLOAD_SSH_KEY}")

# 端口参数大小写不同：ssh 用 -p，scp 用 -P（scp 的 -p 是「保留时间属性」）。
ssh_port=()
scp_port=()
if [[ -n "${NEXO_UPLOAD_SSH_PORT:-}" ]]; then
  ssh_port=(-p "${NEXO_UPLOAD_SSH_PORT}")
  scp_port=(-P "${NEXO_UPLOAD_SSH_PORT}")
fi

dest="${NEXO_UPLOAD_USER}@${NEXO_UPLOAD_HOST}"
# 远程父目录 = prefix/<safe_user_id>，纯 ascii 且已清洗，单引号安全。
remote_parent="$(dirname "${rel_path}")"

# 1. 建远程父目录（scp 不会自动建目录）。
ssh "${ssh_opts[@]}" "${ssh_port[@]}" "${dest}" "mkdir -p '${NEXO_UPLOAD_DIR}/${remote_parent}'"

# 2. 传输文件。dest 用双引号包裹；rel_path 中的空格/CJK 在 SFTP 协议下安全。
scp "${ssh_opts[@]}" "${scp_port[@]}" "${local_file}" "${dest}:${NEXO_UPLOAD_DIR}/${rel_path}"
