"""Tests for the liveness heartbeat + Langfuse tracing setup.

Hermetic: the heartbeat file is redirected to tmp_path so the real data/ tree
is never touched. Langfuse/pydantic-ai side effects are stubbed so no real
backend or credentials are needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

import pytest

import nexo.observability as obs
from nexo.observability import (
    check_heartbeat,
    configure,
    flush,
    start_heartbeat_loop,
    trace_span,
    trace_turn,
    touch_heartbeat,
)


@pytest.fixture(autouse=True)
def _reset_langfuse_flag():
    """Tracing is module-global; reset it so tests don't leak enablement."""
    obs._langfuse_enabled = False
    yield
    obs._langfuse_enabled = False


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


# --- Langfuse configure() gating -------------------------------------------

class _FakeClient:
    """Minimal Langfuse client stub: records auth result + instrument calls."""

    def __init__(self, authenticated: bool = True) -> None:
        self._authenticated = authenticated
        self.flushed = False

    def auth_check(self) -> bool:
        return self._authenticated

    def flush(self) -> None:
        self.flushed = True


@pytest.fixture
def _stub_langfuse(monkeypatch):
    """Stub langfuse + pydantic-ai instrumentation so configure() is hermetic.

    Returns a mutable holder so individual tests can assert on what configure()
    did (whether instrument_all ran, which client was built).
    """
    state: dict = {"instrument_all_called": False, "client": None}

    def fake_get_client():
        state["client"] = _FakeClient()
        return state["client"]

    from pydantic_ai.agent import Agent

    monkeypatch.setattr("langfuse.get_client", fake_get_client)
    monkeypatch.setattr(Agent, "instrument_all", lambda *a, **k: state.__setitem__("instrument_all_called", True))
    return state


def test_configure_disabled_when_no_credentials(monkeypatch):
    """No keys -> tracing stays off and langfuse/pydantic-ai are never touched."""
    monkeypatch.setattr(obs, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(obs, "LANGFUSE_SECRET_KEY", "")
    configure()
    assert obs._langfuse_enabled is False


def test_configure_enables_when_credentials_and_auth_ok(_stub_langfuse, monkeypatch):
    monkeypatch.setattr(obs, "LANGFUSE_PUBLIC_KEY", "pk-lf-xxx")
    monkeypatch.setattr(obs, "LANGFUSE_SECRET_KEY", "sk-lf-xxx")
    configure()
    assert _stub_langfuse["instrument_all_called"] is True
    assert obs._langfuse_enabled is True


def test_configure_disables_when_auth_fails(_stub_langfuse, monkeypatch):
    """Keys present but auth_check() False -> tracing stays off."""
    monkeypatch.setattr(obs, "LANGFUSE_PUBLIC_KEY", "pk-lf-xxx")
    monkeypatch.setattr(obs, "LANGFUSE_SECRET_KEY", "sk-lf-xxx")

    def fake_get_client():
        return _FakeClient(authenticated=False)

    monkeypatch.setattr("langfuse.get_client", fake_get_client)
    configure()
    # instrument_all still runs (creds present), but enablement is gated on auth.
    assert _stub_langfuse["instrument_all_called"] is True
    assert obs._langfuse_enabled is False


def test_configure_disables_when_auth_check_raises(_stub_langfuse, monkeypatch):
    """A network error from auth_check() must not crash the bot — disable instead."""
    monkeypatch.setattr(obs, "LANGFUSE_PUBLIC_KEY", "pk-lf-xxx")
    monkeypatch.setattr(obs, "LANGFUSE_SECRET_KEY", "sk-lf-xxx")

    class _BoomClient:
        def auth_check(self):
            raise TimeoutError("timed out")

    monkeypatch.setattr("langfuse.get_client", lambda: _BoomClient())
    configure()  # must not raise
    assert obs._langfuse_enabled is False


# --- proxy bypass for LAN-hosted Langfuse ----------------------------------

@pytest.mark.parametrize("delenv", [True, False])
def test_bypass_proxy_adds_private_host(monkeypatch, delenv):
    """A private-IP Langfuse host is added to NO_PROXY + no_proxy."""
    if delenv:
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
    else:
        monkeypatch.setenv("NO_PROXY", "10.0.0.0/8")
        monkeypatch.setenv("no_proxy", "")
    obs._bypass_proxy_for_langfuse("http://10.13.11.7:3000")
    assert "10.13.11.7" in os.environ["NO_PROXY"]
    assert "10.13.11.7" in os.environ["no_proxy"]


def test_bypass_proxy_leaves_public_host_alone(monkeypatch):
    """A public hostname must not be force-bypassed (may need the proxy)."""
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    obs._bypass_proxy_for_langfuse("https://cloud.langfuse.com")
    assert os.environ["NO_PROXY"] == ""
    assert os.environ["no_proxy"] == ""


def test_bypass_proxy_handles_localhost_and_empty(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    obs._bypass_proxy_for_langfuse("http://localhost:3000")
    assert "localhost" in os.environ["NO_PROXY"]
    # Empty / unparseable -> no-op (no crash).
    obs._bypass_proxy_for_langfuse("")
    obs._bypass_proxy_for_langfuse("not-a-url")


# --- trace_turn / flush behavior -------------------------------------------

def test_trace_turn_is_noop_when_disabled():
    """When tracing is off, trace_turn yields None and imports nothing."""
    obs._langfuse_enabled = False
    with trace_turn("x", session_id="s", user_id="u", tags=["t"], input="hi") as root:
        assert root is None


def test_trace_turn_wraps_with_langfuse_when_enabled(monkeypatch):
    """When enabled, trace_turn opens a root span, sets input, propagates attrs."""
    obs._langfuse_enabled = True

    updates: list = []
    propagated: list = {}

    class _Root:
        def update(self, **kw):
            updates.append(kw)

    @contextlib.contextmanager
    def fake_start_as_current_observation(*, as_type, name):
        yield _Root()

    @contextlib.contextmanager
    def fake_propagate_attributes(**kw):
        propagated.update(kw)
        yield

    class _Client:
        start_as_current_observation = staticmethod(fake_start_as_current_observation)

    monkeypatch.setattr("langfuse.get_client", lambda: _Client())
    monkeypatch.setattr("langfuse.propagate_attributes", fake_propagate_attributes)

    with trace_turn(
        "WeCom text reply",
        session_id="wecom:2",
        user_id="2",
        tags=["we-com", "text"],
        input="hello",
    ) as root:
        assert isinstance(root, _Root)

    assert updates == [{"input": "hello"}]
    assert propagated == {"session_id": "wecom:2", "user_id": "2", "tags": ["we-com", "text"]}


# --- trace_span behavior ---------------------------------------------------

def test_trace_span_is_noop_when_disabled():
    """When tracing is off, trace_span yields None and imports nothing."""
    obs._langfuse_enabled = False
    with trace_span("download file", input="foo.txt") as span:
        assert span is None


def test_trace_span_opens_child_span_when_enabled(monkeypatch):
    """When enabled, trace_span opens a child span and sets input."""
    obs._langfuse_enabled = True

    updates: list = []
    opened: list = []

    class _Span:
        def update(self, **kw):
            updates.append(kw)

    @contextlib.contextmanager
    def fake_start_as_current_observation(*, as_type, name):
        opened.append((as_type, name))
        yield _Span()

    class _Client:
        start_as_current_observation = staticmethod(fake_start_as_current_observation)

    monkeypatch.setattr("langfuse.get_client", lambda: _Client())

    with trace_span("download file", input="report.pdf") as span:
        assert isinstance(span, _Span)

    assert opened == [("span", "download file")]
    assert updates == [{"input": "report.pdf"}]


def test_trace_span_omits_input_when_none(monkeypatch):
    """No input= -> no update() call (mirrors trace_turn's None guard)."""
    obs._langfuse_enabled = True

    updates: list = []

    class _Span:
        def update(self, **kw):
            updates.append(kw)

    @contextlib.contextmanager
    def fake_start_as_current_observation(*, as_type, name):
        yield _Span()

    class _Client:
        start_as_current_observation = staticmethod(fake_start_as_current_observation)

    monkeypatch.setattr("langfuse.get_client", lambda: _Client())

    with trace_span("download image") as span:
        assert isinstance(span, _Span)

    assert updates == []


def test_trace_span_records_metadata(monkeypatch):
    """metadata= is forwarded to span.update (e.g. the download URL host)."""
    obs._langfuse_enabled = True

    updates: list = []

    class _Span:
        def update(self, **kw):
            updates.append(kw)

    @contextlib.contextmanager
    def fake_start_as_current_observation(*, as_type, name):
        yield _Span()

    class _Client:
        start_as_current_observation = staticmethod(fake_start_as_current_observation)

    monkeypatch.setattr("langfuse.get_client", lambda: _Client())

    with trace_span("download file", input="f.bin", metadata={"url_host": "download.weixin.qq.com"}):
        pass

    assert updates == [{"input": "f.bin", "metadata": {"url_host": "download.weixin.qq.com"}}]


def test_flush_is_noop_when_disabled():
    obs._langfuse_enabled = False
    flush()  # must not raise / must not import langfuse


def test_flush_calls_client_when_enabled(monkeypatch):
    obs._langfuse_enabled = True
    client = _FakeClient()
    monkeypatch.setattr("langfuse.get_client", lambda: client)
    flush()
    assert client.flushed is True
