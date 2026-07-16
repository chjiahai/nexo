"""Centralized prompt & user-facing copy management.

All agent system prompts and WeCom-bubble message templates live in
`prompts.toml` at the project root, so they can be tuned after delivery
without touching code. Loaded once at import time — restart the bot to pick
up edits.

Templates may contain `{placeholder}` substitutions; format them with
`msg(key, **kwargs)`. Plain strings (system prompts, summary template) are
exposed as module-level constants.
"""

from __future__ import annotations

import tomllib

from nexo.config import _ROOT

_PROMPTS_FILE = _ROOT / "prompts.toml"

if not _PROMPTS_FILE.exists():
    raise FileNotFoundError(
        f"prompts.toml not found at {_PROMPTS_FILE}. "
        "Restore it from the repo — it is the source of truth for all prompts."
    )

with _PROMPTS_FILE.open("rb") as _f:
    _data = tomllib.load(_f)


# --- Agent system prompts --------------------------------------------------
# `version` is recorded as `prompt_version` trace metadata (observability) so
# prompt iterations are filterable in Langfuse. Defaults to "1" if absent.
CHAT_SYSTEM_PROMPT: str = _data["chat"]["system_prompt"].strip()
CHAT_SYSTEM_PROMPT_VERSION: str = str(_data["chat"].get("version", "1"))

# --- User-facing message templates ----------------------------------------
_MESSAGES: dict[str, str] = _data["messages"]


def msg(key: str, **placeholders: object) -> str:
    """Return a user-facing message, formatting any `{placeholder}`.

    Always calls `str.format`, so a template with a `{placeholder}` that is
    not supplied raises KeyError (fail loud — a forgotten argument is a bug,
    not a silent literal `{error}` in the bubble). Templates without
    placeholders format to themselves unchanged.
    """
    return _MESSAGES[key].format(**placeholders)
