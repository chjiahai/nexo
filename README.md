# Nexo

Nexo is a scalable open-source agent system for personal and enterprise document intelligence, it transforms unstructured documents into a queryable, memory-aware intelligence layer.

Today Nexo runs as an **enterprise WeCom (企业微信) AI bot**: an outbound WebSocket client that chats with users, and ships the files and images they upload to a remote folder via scp — the first step toward a later document-processing pipeline.

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

## 🧱 Architecture

The codebase is layered, and dependencies point one way (transport → app → agent → storage):

```
src/nexo/
├── api/            transport adapters (WeCom WebSocket SDK) — owns the wire protocol
├── app.py          request routing + session state (the only layer holding cross-turn state)
├── agents/         pure agents (chat: text-in → streamed text-out)
├── storage/        remote-folder storage (scp) for uploaded files & images
├── observability.py  Langfuse LLM tracing + stdlib logging + liveness heartbeat
├── prompts.py      loads prompts.toml — single source of truth for prompts & user copy
├── config.py       env-driven configuration (loaded via python-dotenv)
└── cli.py          `nexo` entry point
```

Routing is **deterministic by message type** — there is no router agent:

```
text  -> handle_text   -> chat_agent (streamed)        [live]
file  -> handle_media  -> remote-folder ship + ack     [live]
image -> handle_media  -> remote-folder ship + ack     [live]
```

---

## 🛠 Local Development

Requires Python ≥ 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # install deps into .venv
cp .env.example .env    # then fill in credentials (see Configuration)
```

### CLI

```bash
nexo            # smoke check (prints hello)
nexo bot        # connect the WeCom bot and serve
nexo health     # liveness probe (fresh heartbeat = healthy)
```

### Tests & lint

```bash
uv run pytest
uv run ruff check .
```

---

## ⚙️ Configuration

All configuration lives in `.env` (gitignored — copy from `.env.example`). Groups:

- **Model** — any OpenAI-compatible endpoint. `NEXO_MODEL` is a pydantic-ai provider-prefixed name (e.g. `openai-chat:deepseek-v4-flash`), with `OPENAI_API_KEY` / `OPENAI_BASE_URL` for credentials. Switching to ZhipuAI (GLM) is a two-line change (documented in `.env.example`).
- **WeCom bot** — `WECHAT_BOT_ID` / `WECHAT_BOT_SECRET` (from the WeCom admin backend) + `WECOM_REQUEST_TIMEOUT_MS` for file/image downloads.
- **Remote upload target** — `NEXO_UPLOAD_HOST` / `NEXO_UPLOAD_USER` / `NEXO_UPLOAD_DIR` (+ optional `NEXO_UPLOAD_SSH_KEY` / `NEXO_UPLOAD_SSH_PORT`). Received files/images are scp'd to this folder via `scripts/ship_media.sh`; nothing is written to local disk.
- **Observability** — `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`. Tracing is enabled only when both keys are set; with neither set it is a no-op (local dev).

### Tuning prompts & wording

`prompts.toml` is the single source of truth for every system prompt and every string shown to users. Edit it freely — no code changes required. Reloaded at bot startup; restart `nexo bot` to apply. Bump `[chat].version` when the system prompt changes so prompt iterations are filterable in Langfuse.

---

## 🐳 Docker 部署

nexo 的运行入口 `nexo bot` 是一个**出站 WebSocket 客户端**(连接企业微信 `wss://openws.work.weixin.qq.com`),不监听任何端口,因此 compose 里没有 `ports:` 映射。

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env,填入 OPENAI_API_KEY / OPENAI_BASE_URL / NEXO_MODEL
# 以及 WECHAT_BOT_ID / WECHAT_BOT_SECRET
# 以及 NEXO_UPLOAD_* (scp 投递上传文件到远程文件夹)
```

`.env` 已被 gitignore,不会进入镜像(由 `.dockerignore` 兜底)。

### 2. 启动

```bash
docker compose up -d        # 后台运行
docker compose logs -f nexo  # 查看日志
docker compose down          # 停止
```

`./data:/app/data` 绑定挂载仅用于持久化**心跳文件**(Docker 健康检查用)。用户上传的文件与图片通过 **scp 投递到远程文件夹**(`NEXO_UPLOAD_*` 配置 + 只读挂载的 SSH 私钥),不写本地磁盘。

### 3. 健康检查

`nexo health` 读取心跳文件判断 WebSocket 是否在线(比 `nexo hello` 只能证明包可导入更可靠),已配置为 compose `healthcheck`;掉线时由 `restart: unless-stopped` 兜底重启。

### 4. 多架构本地构建

```bash
./scripts/publish.sh
```

脚本会构建 `linux/amd64` + `linux/arm64` 双架构镜像,tag 为 `nexo:<version>-<arch>` 和 `nexo:latest-<arch>`,并组装本地 fat manifest `nexo:<version>` / `nexo:latest`,可直接 `docker run --platform linux/arm64 nexo:<version>`。跨架构首次构建走 QEMU 仿真,较慢属正常。如需卸载/重装 QEMU:`docker run --privileged --rm tonistiigi/binfmt --install all`。

### 5. 远程部署到测试机

```bash
cp scripts/deploy.env.example scripts/deploy.env   # 填入 REMOTE_HOST / REMOTE_USER / REMOTE_PATH
bash scripts/deploy.sh                              # SSH 到远端 git pull + docker compose up -d --build
```

推送代码不会自动部署——部署永远是手动的一步。前置:远端已配好 `.env`、`data/` 对 uid 10001 可写、SSH 免密(`ssh-copy-id`)。

### 数据卷权限注意

容器以非 root 用户 `appuser`(uid 10001)运行。首次启动前需让宿主 `./data` 目录对该 uid 可写:

```bash
sudo chown -R 10001:10001 data/
```

或在 `docker-compose.yml` 里加 `user: "${UID:-10001}:${GID:-10001}"` 覆盖为当前用户。

---

## 🔬 可观测性 (Langfuse)

Nexo 通过 [Langfuse](https://langfuse.com) 对 LLM 调用做追踪:pydantic-ai 的 `instrument_all()` 自动把每次 agent 运行(模型名、token 用量、输入输出、延迟)作为 `generation` span 上报,业务侧用 `trace_turn` / `trace_span` 包裹每一轮对话与下载/上传步骤,并带上 `session_id` / `user_id` / tags。

- **云端**:只需在 `.env` 填 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`(`LANGFUSE_BASE_URL` 留空即用默认云)。
- **自托管**:仓库提供 `docker-compose.langfuse.yml`(基于官方 v3.213.0,含 Postgres / ClickHouse / Redis / MinIO)。注意里面所有 `# CHANGEME` 的密钥已用 `openssl` 随机生成,**正式环境请自行替换**。

  ```bash
  docker compose -f docker-compose.langfuse.yml up -d
  # Web UI: http://localhost:3000  (先在 UI 里建项目并生成 API Keys)
  ```

  然后把 `.env` 里的 `LANGFUSE_BASE_URL` 指向自托管地址(如 `http://10.13.11.7:3000`)。若你设了 `HTTP(S)_PROXY` 访问外部 LLM,而 Langfuse 在内网私网 IP 上,Nexo 会自动把该私网地址加入 `NO_PROXY`,直连不走代理。

追踪未启用时(未设密钥)所有追踪调用都是 no-op,bot 正常运行。

---
