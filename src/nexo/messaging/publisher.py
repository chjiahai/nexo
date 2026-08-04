"""JetStream publisher for the "rich event" — drain → archive / future subscribers.

`nexo drain` publishes one rich event per processed WeCom message: the original
frame plus the result of processing (the LLM reply text for text messages, the
OBS object key for media, an error string on failure, and org/bot ids). The
event is published to `<prefix>.<msgtype>` on the `WECOM_MSG` stream, where
`nexo archive` (and future OCR/summary subscribers) consume it.

Publishing RAISES on failure: drain must not mark the outbox row done until the
JetStream publish is acked, so a failed publish leaves the row pending and drain
retries it on the next loop (idempotent on the consumer side via `nats_seq`).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext

from nexo.config import NATS_STREAM, NATS_SUBJECT_PREFIX, NATS_URL

logger = logging.getLogger("nexo.messaging")


class EventPublisher:
    """Owns a NATS connection + JetStream context for publishing rich events."""

    def __init__(self) -> None:
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None

    async def connect(self) -> None:
        self._nc = await nats.connect(NATS_URL)
        self._js = self._nc.jetstream()
        logger.info("Connected to NATS %s (stream=%s)", NATS_URL, NATS_STREAM)

    async def publish(self, event: dict[str, Any]) -> int:
        """Publish one rich event to `<prefix>.<msgtype>`; returns the stream seq.

        Raises on any failure so the caller (drain) leaves the outbox row
        pending for retry. No-op (returns 0) until `connect()` has run, so
        callers/tests that don't connect simply skip.
        """
        if self._js is None:
            return 0

        frame = event.get("frame", {}) or {}
        msgtype = frame.get("body", {}).get("msgtype", "unknown") or "unknown"
        subject = f"{NATS_SUBJECT_PREFIX}.{msgtype}"
        data = json.dumps(event, ensure_ascii=False).encode("utf-8")
        ack = await self._js.publish(subject, data, stream=NATS_STREAM)
        logger.debug("Published event to %s (seq=%s)", subject, ack.seq)
        return ack.seq

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._js = None
