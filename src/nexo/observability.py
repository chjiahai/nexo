"""Langfuse LLM tracing + stdlib logging + process-liveness heartbeat.

Langfuse tracing is OTel-based: `configure()` initializes the Langfuse client
(which registers an OpenTelemetry exporter pointing at Langfuse) and calls
`Agent.instrument_all()` so pydantic-ai auto-traces every agent run — model
name, token usage, input/output and latency land as a `generation` span with
zero per-call code. See https://langfuse.com/integrations/frameworks/pydantic-ai

Tracing is enabled only when `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` are
set; otherwise it is a no-op (local dev, tests). `langfuse` is imported lazily
inside `configure()`/`trace_turn()` — AFTER `config.py` has run `load_dotenv`,
so the SDK sees the credentials (importing it before env vars load initializes
it with missing/wrong creds).

The liveness heartbeat is separate from tracing: the bot is an outbound
WebSocket client with no listening port, so we write a heartbeat file while the
WS is connected and `nexo health` checks its freshness — that becomes the
Docker healthcheck.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

from nexo.config import (
    DATA_DIR,
    LANGFUSE_BASE_URL,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
)

logger = logging.getLogger("nexo.observability")

# Set True by configure() when Langfuse credentials are present. trace_turn()
# and flush() consult this so callers (wecom.py) stay unconditional — no
# tracing code paths to gate at the call sites, and tests (which never call
# configure()) get pure no-ops.
_langfuse_enabled = False

# Heartbeat file lives under data/ (already gitignored). Touched while the WS is
# connected; `nexo health` reads it to decide liveness.
_HEARTBEAT_FILE = DATA_DIR / ".heartbeat"
_HEARTBEAT_INTERVAL = 30  # seconds between heartbeat ticks
_HEALTH_MAX_AGE = 90  # seconds; heartbeat staler than this = unhealthy


def configure() -> None:
    """Initialize stdlib logging + Langfuse tracing. Call once at bot startup.

    Routes INFO-level logs to STDERR (line-buffered; stdout is block-buffered
    when not a TTY, which would sit on the buffer until process exit —
    invisible in a long-running bot). Enables Langfuse tracing + pydantic-ai
    auto-instrumentation when credentials are present, else logs once and
    leaves tracing disabled. Never raises: any Langfuse setup/auth failure
    (including a backend timeout) disables tracing and lets the bot start.
    """
    global _langfuse_enabled
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    if not (LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY):
        logger.info("Langfuse credentials not set; tracing disabled")
        return

    # A LAN-hosted Langfuse (private/loopback IP) must be reached directly:
    # httpx (trust_env=True) would otherwise route the request through a shell
    # HTTP(S)_PROXY (commonly set to reach external LLM APIs), and the proxy
    # can't reach a private address -> ReadTimeout. Public hostnames are left
    # alone so cloud Langfuse reached via a proxy still works.
    _bypass_proxy_for_langfuse(LANGFUSE_BASE_URL)

    try:
        # Imported lazily so the SDK initializes AFTER load_dotenv (config.py)
        # has populated LANGFUSE_* in os.environ.
        from langfuse import get_client
        from pydantic_ai.agent import Agent

        client = get_client()  # reads LANGFUSE_* from the environment
        # instrument_all() must run before any agent run; it enables pydantic-ai
        # to emit OTel spans, which the Langfuse client's exporter ships to Langfuse.
        Agent.instrument_all()

        if client.auth_check():
            _langfuse_enabled = True
            logger.info("Langfuse tracing enabled -> %s", LANGFUSE_BASE_URL or "cloud")
        else:
            # Credentials present but rejected — keep tracing off rather than
            # buffering spans that can never export.
            logger.warning("Langfuse auth failed; tracing disabled (check keys/base URL)")
    except Exception:  # noqa: BLE001 — tracing must never prevent the bot from starting
        logger.warning("Langfuse setup failed; tracing disabled", exc_info=True)


def _bypass_proxy_for_langfuse(base_url: str) -> None:
    """Add a private/loopback Langfuse host to NO_PROXY/no_proxy in os.environ.

    No-op for public hostnames (the user may legitimately proxy to reach cloud
    Langfuse) and for empty/unparseable URLs.
    """
    if not base_url:
        return
    host = urllib.parse.urlparse(base_url).hostname
    if not host:
        return
    if host != "localhost":
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return  # hostname: don't touch proxy routing
        if not (ip.is_private or ip.is_loopback):
            return
    for var in ("NO_PROXY", "no_proxy"):
        current = [h.strip() for h in os.environ.get(var, "").split(",") if h.strip()]
        if host not in current:
            current.append(host)
            os.environ[var] = ",".join(current)


@contextlib.contextmanager
def trace_turn(
    name: str,
    *,
    session_id: str,
    user_id: str,
    tags: list[str],
    input: Any = None,
) -> Iterator[Any]:
    """Wrap a WeCom turn in a Langfuse trace root with session/user context.

    Creates a root observation named `name` and propagates `session_id`/
    `user_id`/`tags` to every nested observation (the pydantic-ai generation
    span becomes a child automatically via OTel context propagation). `input`
    is set explicitly to the relevant user-facing payload (the user message /
    filename) — not all function args — so traces stay readable and don't leak
    configs. Yields the root observation (or None when tracing is disabled).

    No-op when tracing is disabled, so call sites in wecom.py don't need to
    branch on configuration.
    """
    if not _langfuse_enabled:
        yield None
        return

    from langfuse import get_client, propagate_attributes

    with get_client().start_as_current_observation(as_type="span", name=name) as root:
        if input is not None:
            root.update(input=input)
        with propagate_attributes(
            session_id=session_id,
            user_id=user_id,
            tags=tags,
        ):
            yield root


@contextlib.contextmanager
def trace_span(
    name: str,
    *,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open a child span under the current trace (e.g. a download or upload step).

    Nests under the active `trace_turn` root automatically via OTel context
    propagation (the same mechanism by which the pydantic-ai generation span
    becomes a child of the turn). Unlike `trace_turn`, this does not propagate
    session/user/tags — those are already on the parent root. `metadata` is for
    diagnostic key/values (e.g. the download URL host — never the signed URL).
    No-op (yields None) when tracing is disabled, so call sites don't branch.
    """
    if not _langfuse_enabled:
        yield None
        return

    from langfuse import get_client

    with get_client().start_as_current_observation(as_type="span", name=name) as span:
        update: dict[str, Any] = {}
        if input is not None:
            update["input"] = input
        if metadata:
            update["metadata"] = metadata
        if update:
            span.update(**update)
        yield span


def flush() -> None:
    """Flush buffered Langfuse events. Call on shutdown so traces are sent."""
    if not _langfuse_enabled:
        return
    from langfuse import get_client

    get_client().flush()


# --- Liveness heartbeat ----------------------------------------------------

def touch_heartbeat() -> None:
    """Stamp the heartbeat file with the current monotonic-ish timestamp."""
    _HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HEARTBEAT_FILE.write_text(str(time.time()), encoding="utf-8")


def check_heartbeat(max_age: float = _HEALTH_MAX_AGE) -> bool:
    """True if the heartbeat file exists and is fresher than `max_age` seconds."""
    if not _HEARTBEAT_FILE.exists():
        return False
    try:
        ts = float(_HEARTBEAT_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    return (time.time() - ts) <= max_age


async def _heartbeat_loop(is_connected: Callable[[], bool]) -> None:
    """Background task: touch the heartbeat file while the WS stays connected."""
    while True:
        try:
            if is_connected():
                touch_heartbeat()
        except Exception:  # noqa: BLE001 — the heartbeat loop must never die
            logger.warning("heartbeat tick failed", exc_info=True)
        await asyncio.sleep(_HEARTBEAT_INTERVAL)


def start_heartbeat_loop(is_connected: Callable[[], bool]) -> asyncio.Task[None]:
    """Start the liveness heartbeat.

    `is_connected` is a zero-arg callable returning the WS client's connected
    status (the SDK exposes `WSClient.is_connected`).
    """
    return asyncio.create_task(_heartbeat_loop(is_connected))
