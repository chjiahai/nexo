"""Process-liveness heartbeat + stdlib logging setup.

The bot is an outbound WebSocket client with no listening port, so we write a
heartbeat file while the WS is connected and `nexo health` checks its freshness
— that becomes the Docker healthcheck.

`configure()` sets up stdlib logging (level INFO to stderr). Call it once at
bot startup.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable

from nexo.config import DATA_DIR

logger = logging.getLogger("nexo.observability")

# Heartbeat file lives under data/ (already gitignored). Touched while the WS is
# connected; `nexo health` reads it to decide liveness.
_HEARTBEAT_FILE = DATA_DIR / ".heartbeat"
_HEARTBEAT_INTERVAL = 30  # seconds between heartbeat ticks
_HEALTH_MAX_AGE = 90  # seconds; heartbeat staler than this = unhealthy


def configure() -> None:
    """Initialize stdlib logging. Call once at bot startup.

    Routes INFO-level logs to STDERR. STDERR is line-buffered by default; stdout
    is block-buffered when not a TTY (Docker logs, pipe capture), which would sit
    on the buffer until process exit — invisible in a long-running bot.
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)


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
