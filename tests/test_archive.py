"""Tests for `nexo archive` — rich-event → MySQL row extraction.

Covers `_row_from_event` (the mapping from a rich event to the 18 typed columns)
without touching MySQL or NATS. Verifies the new org_id/bot_id/obs_key/reply_text
fields ride through, and that media fields are extracted for file/image/video.
"""

from __future__ import annotations

from nexo.archive import _row_from_event


def _text_event(reply: str = "hi there") -> dict:
    return {
        "frame": {
            "headers": {"req_id": "req-1"},
            "body": {
                "msgid": "mid-1", "msgtype": "text", "chattype": "single",
                "from": {"userid": "2"}, "text": {"content": "hello"},
            },
        },
        "reply_text": reply, "obs_key": None, "error": None,
        "org_id": "org1", "bot_id": "bot1",
    }


def test_text_event_extracts_content_and_reply():
    row = _row_from_event(_text_event("the reply"), "WECOM_MSG", 42)
    # frame-extracted fields
    assert row[0] == "mid-1"          # msgid
    assert row[1] == "text"           # msgtype
    assert row[2] == "single"         # chattype
    assert row[3] == "wecom:2"        # session_id
    assert row[4] == "2"              # user_id
    assert row[5] is None             # chat_id (single chat)
    assert row[6] == "hello"          # content
    assert row[10] == "req-1"         # req_id
    # rich-event fields
    assert row[12] == "org1"          # org_id
    assert row[13] == "bot1"          # bot_id
    assert row[14] is None            # obs_key (text)
    assert row[15] == "the reply"     # reply_text
    assert row[16] == "the reply"     # reply_text again (CASE WHEN %s IS NULL)
    # nats idempotency key
    assert row[17] == "WECOM_MSG"
    assert row[18] == 42


def test_group_chat_sets_chat_id_not_user_id():
    event = _text_event()
    event["frame"]["body"]["chattype"] = "group"
    event["frame"]["body"]["chatid"] = "group-xyz"
    event["frame"]["body"].pop("from", None)
    row = _row_from_event(event, "WECOM_MSG", 1)
    assert row[2] == "group"
    assert row[4] is None             # user_id (group)
    assert row[5] == "group-xyz"      # chat_id


def test_file_event_extracts_media_fields_and_obs_key():
    event = {
        "frame": {
            "headers": {"req_id": "r"},
            "body": {
                "msgid": "mid-f", "msgtype": "file", "chattype": "single",
                "from": {"userid": "u1"},
                "file": {"url": "https://x/y.pdf", "aeskey": "key==", "filename": "doc.pdf"},
            },
        },
        "reply_text": None, "obs_key": "org/u1/mid-f-doc.pdf", "error": None,
        "org_id": "org1", "bot_id": "bot1",
    }
    row = _row_from_event(event, "WECOM_MSG", 7)
    assert row[1] == "file"
    assert row[7] == "https://x/y.pdf"      # media_url
    assert row[8] == "key=="                # media_aeskey
    assert row[9] == "doc.pdf"             # filename
    assert row[14] == "org/u1/mid-f-doc.pdf"  # obs_key
    assert row[15] is None                 # reply_text (media ack not recorded here)
    assert row[16] is None                 # CASE placeholder mirror


def test_image_event_has_no_filename():
    event = {
        "frame": {"headers": {"req_id": "r"},
                  "body": {"msgid": "m", "msgtype": "image", "chattype": "single",
                           "from": {"userid": "u"},
                           "image": {"url": "https://i/p", "aeskey": "k="}}},
        "reply_text": None, "obs_key": "org/u/m-image.png", "error": None,
        "org_id": "o", "bot_id": "b",
    }
    row = _row_from_event(event, "WECOM_MSG", 1)
    assert row[1] == "image"
    assert row[9] is None              # filename not extracted for image
    assert row[7] == "https://i/p"
