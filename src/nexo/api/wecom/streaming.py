"""WeCom bubble streaming protocol.

`_reply_streamed` pumps an async generator of reply chunks back to WeCom,
which REPLACES bubble content on each refresh (it does not append) — every
frame carries the FULL text the bubble should show. Two modes:

- accumulate=True (chat): each frame carries the GROWING accumulated text, so
  the streamed reply reads like a growing prefix. The finish frame carries the
  full reply.
- accumulate=False (file/image route): each chunk REPLACES the previous one, so
  the staged progress messages ("正在下载…" -> "正在保存…") show transiently and
  are wiped when the next stage appears. The final chunk replaces all progress,
  exactly like a chat reply.

The queue+pump design lets timeouts fire on `queue.get()` (safe to cancel)
rather than on the generator's `__anext__` (which would close it and lose the
rest of the stream). Kept separate from routing so the protocol is testable in
isolation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from aibot import WSClient, generate_req_id

from nexo.prompts import msg

logger = logging.getLogger("nexo.wecom")

# Flush buffered tokens either when they reach this size, or after this delay
# (seconds) — whichever comes first. The SDK acks every reply_stream call
# serially per req_id, so sending one frame per token would be slow.
_FLUSH_BYTES = 64
_FLUSH_INTERVAL = 0.4


async def _reply_streamed(
    ws_client: WSClient,
    frame: dict[str, Any],
    chunks: AsyncIterator[str],
    *,
    accumulate: bool = True,
) -> str:
    """Stream an async generator of reply chunks back to WeCom.

    On error, send a finish frame preserving whatever was last shown. Returns
    the final text sent to the bubble (the full reply, or the error text) so
    the caller can attach it as the trace root's output.
    """
    stream_id = generate_req_id("stream")

    async def _send(content: str, finish: bool) -> None:
        await ws_client.reply_stream(frame, stream_id, content, finish=finish)

    # `last_content` is whatever the bubble currently shows — used both to skip
    # redundant frames and to preserve partial text on error.
    last_content = ""
    full: list[str] = []
    final_text = ""

    try:
        await _send(msg("thinking"), finish=False)
        last_content = msg("thinking")

        if accumulate:
            pending = 0  # bytes accumulated since the last flush
            queue: asyncio.Queue = asyncio.Queue()
            eos = object()  # end-of-stream sentinel pushed by the pump

            async def _pump() -> None:
                # Own the chunks generator's lifetime so a slow producer is never
                # cancelled: timeouts fire on queue.get() (safe to cancel) rather
                # than on __anext__ — cancelling __anext__ closes the async
                # generator and loses the rest of the stream.
                try:
                    async for chunk in chunks:
                        await queue.put(chunk)
                except Exception as exc:  # surface producer errors to the consumer
                    await queue.put(exc)
                finally:
                    await queue.put(eos)

            pump = asyncio.create_task(_pump())
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=_FLUSH_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        # Time-based flush: surface accumulated bytes before they
                        # reach _FLUSH_BYTES, so a slow-streaming model doesn't
                        # leave the bubble stuck on the previous frame.
                        if pending > 0:
                            text = "".join(full)
                            if text != last_content:
                                await _send(text, finish=False)
                                last_content = text
                            pending = 0
                        continue
                    if item is eos:
                        break
                    if isinstance(item, Exception):
                        raise item
                    full.append(item)
                    pending += len(item)
                    if pending >= _FLUSH_BYTES:
                        text = "".join(full)
                        if text != last_content:
                            await _send(text, finish=False)
                            last_content = text
                        pending = 0
            finally:
                # If the consumer exited early (e.g. _send failed), stop the pump
                # so it doesn't leak; otherwise it has already finished. Swallow
                # whatever the cancelled pump raises so it can't mask the
                # exception already propagating from the consumer.
                if not pump.done():
                    pump.cancel()
                    try:
                        await pump
                    except BaseException:  # noqa: BLE001 — see comment above
                        pass
            last_content = "".join(full) or msg("no_reply")
        else:
            # Each chunk stands alone as the full bubble; WeCom replaces, so the
            # previous stage vanishes. The final chunk is re-sent as the finish
            # frame below (one redundant frame, harmless).
            async for chunk in chunks:
                if chunk and chunk != last_content:
                    await _send(chunk, finish=False)
                    last_content = chunk

        # Finish frame must carry the final text (an empty finish frame would
        # wipe the bubble).
        final_text = last_content or msg("no_reply")
        await _send(final_text, finish=True)

    except Exception as exc:  # noqa: BLE001 — must not leave the bubble hanging
        logger.exception("Error while handling WeCom message")
        try:
            # Preserve whatever was generated: in accumulate mode that's the full
            # joined text (including sub-threshold bytes never flushed); in
            # replace mode it's the last stage shown.
            partial = "".join(full) if accumulate else last_content
            err_text = (
                f"{partial}\n\n[出错] {exc}".strip()
                if partial
                else f"[出错] {exc}"
            )
            await _send(err_text, finish=True)
            final_text = err_text
        except Exception:
            logger.exception("Failed to send error frame to WeCom")

    return final_text
