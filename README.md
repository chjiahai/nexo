# Nexo

Nexo is a scalable open-source agent system for personal and enterprise document intelligence, it transforms unstructured documents into a queryable, memory-aware intelligence layer.

---

## 🧠 Project Philosophy

Nexo is built on three core principles:

- **Documents are memory**
- **Retrieval is ground truth**
- **Agents are orchestration layer, not chatbots**

---

## 🧠 Core Idea

> Documents are not files. They are memory nodes in an intelligence system.

Nexo connects them into a queryable, evolving knowledge space.

---

## 🐳 Docker 部署

nexo 的运行入口 `nexo bot` 是一个**出站 WebSocket 客户端**(连接企业微信 `wss://openws.work.weixin.qq.com`),不监听任何端口,因此 compose 里没有 `ports:` 映射。

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env,填入 OPENAI_API_KEY / OPENAI_BASE_URL / NEXO_MODEL
# 以及 WECHAT_BOT_ID / WECHAT_BOT_SECRET
```

`.env` 已被 gitignore,不会进入镜像(由 `.dockerignore` 兜底)。

### 2. 启动

```bash
docker compose up -d        # 后台运行
docker compose logs -f nexo  # 查看日志
docker compose down          # 停止
```

运行时数据(uploads / processed / index)通过 `./data:/app/data` 绑定挂载持久化。

### 3. 多架构本地构建

```bash
./scripts/publish.sh
```

脚本会构建 `linux/amd64` + `linux/arm64` 双架构镜像,tag 为 `nexo:<version>-<arch>` 和 `nexo:latest-<arch>`,并组装本地 fat manifest `nexo:<version>` / `nexo:latest`,可直接 `docker run --platform linux/arm64 nexo:<version>`。跨架构首次构建走 QEMU 仿真,较慢属正常。如需卸载/重装 QEMU:`docker run --privileged --rm tonistiigi/binfmt --install all`。

### 数据卷权限注意

容器以非 root 用户 `appuser`(uid 10001)运行。首次启动前需让宿主 `./data` 目录对该 uid 可写:

```bash
sudo chown -R 10001:10001 data/
```

或在 `docker-compose.yml` 里加 `user: "${UID:-10001}:${GID:-10001}"` 覆盖为当前用户。

---
