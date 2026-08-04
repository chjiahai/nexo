-- wecom_messages: every WeCom message (rich event), persisted by `nexo archive`.
--
-- Typed columns mirror the extractors in src/nexo/api/wecom/frames.py;
-- raw_payload keeps the full original frame for audit/future fields.
-- Idempotency: UNIQUE (nats_stream, nats_seq) — JetStream redelivery never
-- duplicates a row (the sink uses INSERT IGNORE).

CREATE TABLE IF NOT EXISTS wecom_messages (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  msgid           VARCHAR(128) NULL,                    -- WeCom message id (frame.body.msgid)
  msgtype         VARCHAR(32)  NOT NULL,                -- text/file/image/video/mixed/voice/...
  chattype        VARCHAR(16)  NULL,                    -- single | group
  session_id      VARCHAR(128) NULL,                    -- derived: wecom:{userid} or wecom:{chatid}
  user_id         VARCHAR(128) NULL,                    -- single-chat sender userid (frame.body.from.userid)
  chat_id         VARCHAR(128) NULL,                    -- group chatid (frame.body.chatid)
  content         TEXT         NULL,                    -- text content (frame.body.text.content)
  media_url       VARCHAR(512) NULL,                    -- media download url (body.<kind>.url)
  media_aeskey    VARCHAR(256) NULL,                    -- AES-256-CBC key, base64 (body.<kind>.aeskey)
  filename        VARCHAR(255) NULL,                    -- file name (body.file.{filename|name|file_name})
  req_id          VARCHAR(128) NULL,                    -- trace id (frame.headers.req_id)
  raw_payload     JSON         NOT NULL,                -- full original frame JSON
  org_id          VARCHAR(64)  NULL,                    -- nexo org grouping (富事件带,多 org 共库区分)
  bot_id          VARCHAR(64)  NULL,                    -- bot id (富事件带,多 bot 共库区分)
  obs_key         VARCHAR(512) NULL,                    -- media 落华为云 OBS 对象 key(富事件直带)
  reply_text      TEXT         NULL,                    -- 出站回复(text LLM 输出 / media 简短回执)
  reply_at        DATETIME(3)  NULL,                    -- 回复时间
  nats_stream     VARCHAR(64)  NULL,                    -- source JetStream stream (WECOM_MSG)
  nats_seq        BIGINT UNSIGNED NULL,                 -- stream sequence — idempotency key
  received_at     DATETIME(3)  NOT NULL,                -- sink insert time (ms precision)
  PRIMARY KEY (id),
  UNIQUE KEY uk_nats (nats_stream, nats_seq),
  KEY idx_msgid (msgid),
  KEY idx_session (session_id, received_at),
  KEY idx_msgtype (msgtype, received_at),
  KEY idx_obs (obs_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
