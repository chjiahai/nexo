"""Observability: logfire + OpenTelemetry, plus process-liveness heartbeat.

logfire (built on the OpenTelemetry SDK) takes over logging and tracing:
- stdlib `logger.info(...)` calls are bridged into OTel via `LogfireLoggingHandler`,
  so existing `wecom.py` logging is unchanged — same calls, now flowing into the
  same observability pipeline.
- pydantic-ai agent runs and httpx (DeepSeek) calls are auto-instrumented, giving
  full traces of the message -> download -> parse -> LLM -> reply chain with zero
  manual span code.

Liveness is separate from logfire: the bot is an outbound WebSocket client with no
listening port, so we write a heartbeat file and `nexo health` checks its
freshness — that becomes the Docker healthcheck.

Two modes, switched by `NEXO_OTEL_ENDPOINT`:
- empty  -> Phase 1 local mode (rich console only, nothing leaves the process)
- set    -> Phase 2: export traces/metrics via OTLP/HTTP to a self-hosted backend
            (e.g. grafana/otel-lgtm).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable

import logfire
from logfire import ConsoleOptions

from nexo.config import DATA_DIR

logger = logging.getLogger("nexo.observability")

# Heartbeat file lives under data/ (already gitignored). Touched while the WS is
# connected; `nexo health` reads it to decide liveness.
_HEARTBEAT_FILE = DATA_DIR / ".heartbeat"
_HEARTBEAT_INTERVAL = 30  # seconds between heartbeat ticks
_HEALTH_MAX_AGE = 90  # seconds; heartbeat staler than this = unhealthy


def configure() -> None:
    """Initialize logfire + OTel instrumentation. Call once at bot startup."""
    otel_endpoint = os.getenv("NEXO_OTEL_ENDPOINT", "").strip()

    # Route logfire's rich console to STDERR (line-buffered by default), not
    # stdout. stdout is block-buffered when not a TTY (Docker logs, pipe
    # capture), which would sit on the buffer until process exit — invisible in
    # a long-running bot. The SDK's own logs already go to stderr; match that.
    console = ConsoleOptions(output=sys.stderr)

    if otel_endpoint:
        # Phase 2 — export traces + metrics to a self-hosted OTel backend.
        logfire.configure(
            send_to_logfire=False,
            service_name="nexo",
            console=console,
            additional_span_processors=[_otlp_span_processor(otel_endpoint)],
            metrics=logfire.MetricsOptions(
                metric_readers=[_otlp_metric_reader(otel_endpoint)]
            ),
        )
        logger.info("OTel export enabled -> %s", otel_endpoint)
    else:
        # Phase 1 — local mode: rich console output, nothing leaves the process.
        logfire.configure(send_to_logfire=False, service_name="nexo", console=console)

    # Match the old `basicConfig(level=INFO)` behavior: let stdlib INFO records
    # through (the LogfireLoggingHandler doesn't filter on level itself).
    logging.getLogger().setLevel(logging.INFO)

    # Bridge stdlib logging into logfire/OTel. Existing logger.info() calls
    # (wecom.py etc.) now flow into the same pipeline, unchanged.
    logging.getLogger().addHandler(logfire.LogfireLoggingHandler())

    # Auto-trace pydantic-ai: agent runs, LLM calls, tool calls, validation, retries.
    logfire.instrument_pydantic_ai()
    # Auto-trace DeepSeek's httpx calls — closes the "API failure untracked" gap.
    logfire.instrument_httpx()


def _otlp_span_processor(endpoint: str):
    """Build a BatchSpanProcessor exporting to `<endpoint>/v1/traces` (OTLP/HTTP)."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))


def _otlp_metric_reader(endpoint: str):
    """Build a metric reader exporting to `<endpoint>/v1/metrics` (OTLP/HTTP)."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    return PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"))


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
