"""WeCom (企业微信) AI bot transport adapter.

A package, split by concern:

- `frames`    — pure frame-parsing helpers (extract text, filenames, media
                fields, session ids) + debug-frame capture.
- `streaming` — the WeCom bubble streaming protocol (`_reply_streamed`): pumps
                an async generator of reply chunks back over the WebSocket.
- `handlers`  — SDK event wiring + connection lifecycle: dispatches by WeCom
                message type, streams replies, and owns connect/disconnect and
                the liveness heartbeat.

The wecom-aibot-python-sdk is a WebSocket *client* that connects out to
wss://openws.work.weixin.qq.com. This adapter bridges it to the application
layer, dispatching by WeCom message type (deterministic routing — no router
agent):

    message.text  ──> app.handle_text  ──> chat_agent (streamed) + outbox
    message.file  ──> _handle_media(file)   ──> download + stage + outbox + ack
    message.image ──> _handle_media(image)  ──> download + stage + outbox + ack
    message (catch-all) ──> video ──> _handle_media(video) ──> download + stage + outbox + ack

The bot is thin: text runs the LLM inline and writes an outbox intent; media is
downloaded (while the WeCom signed URL is fresh), staged locally, and enqueued
— then a short ack is sent. `nexo drain` (separate process) uploads media to
OBS and publishes the rich event to NATS; `nexo archive` records it to MySQL.

The SDK only emits typed events for text/file/image/mixed/voice; other msgtypes
(video, ...) are dropped at a debug log. A catch-all `message` listener routes
the rest — currently video. (Location / WeDrive file never reach the bot:
WeCom replies "目前不支持理解此类型消息" server-side without forwarding.)

This is a transport adapter in the API layer — it knows about the SDK wire
protocol and delegates all agent/session logic to `nexo.app`.
"""

from nexo.api.wecom.handlers import build_client, register_handlers, run

__all__ = ["build_client", "register_handlers", "run"]
