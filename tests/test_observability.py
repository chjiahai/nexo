"""Tests for observability: heartbeat liveness + logfire configure.

Hermetic: heartbeat file is redirected to tmp_path so the real data/ tree is
never touched. `configure()` is exercised to ensure it doesn't blow up on
import/startup (it must not require a live OTel backend or a logfire token).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from nexo.observability import (
    check_heartbeat,
    configure,
    start_heartbeat_loop,
    touch_heartbeat,
)


@pytest.fixture(autouse=True)
def _redirect_heartbeat(tmp_path: Path, monkeypatch):
    """Point the heartbeat file at tmp_path so tests don't touch real data/."""
    monkeypatch.setattr("nexo.observability._HEARTBEAT_FILE", tmp_path / ".heartbeat")
    yield


def test_check_heartbeat_missing_file_is_false():
    assert check_heartbeat() is False


def test_check_heartbeat_fresh_is_true():
    touch_heartbeat()
    assert check_heartbeat() is True


def test_check_heartbeat_stale_is_false():
    """A heartbeat older than max_age seconds is unhealthy."""
    touch_heartbeat()
    # Backdate the file beyond the default 90s window.
    stale_ts = time.time() - 120
    from nexo.observability import _HEARTBEAT_FILE
    _HEARTBEAT_FILE.write_text(str(stale_ts), encoding="utf-8")
    assert check_heartbeat() is False


def test_check_heartbeat_corrupt_file_is_false():
    from nexo.observability import _HEARTBEAT_FILE
    _HEARTBEAT_FILE.write_text("not-a-number", encoding="utf-8")
    assert check_heartbeat() is False


def test_check_heartbeat_respects_max_age():
    touch_heartbeat()
    # Just-stale at a tight threshold, fresh at a loose one.
    from nexo.observability import _HEARTBEAT_FILE
    _HEARTBEAT_FILE.write_text(str(time.time() - 5), encoding="utf-8")
    assert check_heartbeat(max_age=1) is False
    assert check_heartbeat(max_age=10) is True


def test_configure_does_not_raise():
    """configure() must work in local mode (no token, no backend). Idempotent."""
    configure()  # should not raise
    configure()  # second call should also be safe


def test_heartbeat_loop_touches_file_while_connected(monkeypatch):
    """The background loop stamps the file when the WS reports connected."""
    async def _run():
        # Use a tiny interval via monkeypatch so the test is fast.
        import nexo.observability as obs
        monkeypatch.setattr(obs, "_HEARTBEAT_INTERVAL", 0.05)
        task = start_heartbeat_loop(lambda: True)
        await asyncio.sleep(0.12)  # let ~2 ticks fire
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert check_heartbeat() is True


def test_heartbeat_loop_skips_when_disconnected(monkeypatch):
    """When the WS is not connected, the heartbeat is NOT touched."""
    from nexo.observability import _HEARTBEAT_FILE

    async def _run():
        import nexo.observability as obs
        monkeypatch.setattr(obs, "_HEARTBEAT_INTERVAL", 0.05)
        task = start_heartbeat_loop(lambda: False)
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    # File was never written because is_connected() returned False.
    assert not _HEARTBEAT_FILE.exists()
