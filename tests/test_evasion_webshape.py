"""Tests for web-session fingerprint lacing (evasion/webshape.py wiring).

House conventions (mirrors test_web_session.py):
- the scope gate seam (utils.gated_http.check_scan) is stubbed in-process;
- the cookie jar is isolated to a temp COOKIE_JAR_DB per test;
- gated_request is faked to record the headers actually built.

pytest-compatible AND directly runnable (fixture tests need pytest).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evasion.webshape as webshape
import utils.gated_http as gh
import auxiliaries.web_session as ws
from evasion.webshape import (
    LEGACY_UA, HostFloor, host_floor_seconds, sticky_ua,
)


def _load_module(rel, name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _drive_get(monkeypatch, tmp_path, url="http://example.com/", **kw):
    """session_get with faked gated_request; returns (envelope, headers)."""
    ws_mod = _load_module("auxiliaries/web_session.py", "auxiliaries.web_session")
    monkeypatch.setattr(gh, "check_scan", lambda *a, **k: (True, ""))
    recorded = {}

    class _Resp:
        status_code, reason = 200, "OK"
        headers = {"Content-Type": "text/html"}
        text = "<html><title>hi</title></html>"
        content = b"<html></html>"

    def fake_gated_request(method, url, **kwargs):
        recorded["headers"] = dict(kwargs.get("headers") or {})
        return _Resp(), [{"url": url, "status": 200}]

    monkeypatch.setattr(ws_mod, "gated_request", fake_gated_request)
    monkeypatch.setenv("COOKIE_JAR_DB", str(tmp_path / "jar.db"))

    def _fake_store(*a, **k):
        return []
    monkeypatch.setattr(ws_mod.get_jar(), "store_response_cookies", _fake_store)

    env = ws_mod.session_get(url, **kw)
    return env, recorded["headers"]


# --- sticky UA unit paths ------------------------------------------------------


def test_sticky_ua_deterministic_per_host():
    assert sticky_ua("example.com") == sticky_ua("example.com")


def test_sticky_ua_pool_realism():
    for ua in webshape._UA_POOL:
        assert ua.startswith("Mozilla/5.0 ("), ua
    assert len(webshape._UA_POOL) >= 3


def test_session_ua_env_legacy(monkeypatch):
    monkeypatch.setenv("SESSION_UA", "legacy")
    assert sticky_ua("example.com") == LEGACY_UA


def test_session_ua_env_custom(monkeypatch):
    monkeypatch.setenv("SESSION_UA", "audit-probe/1.0")
    assert sticky_ua("whatever.net") == "audit-probe/1.0"


# --- wiring through session_get ------------------------------------------------


def test_session_get_applies_pool_ua(monkeypatch, tmp_path):
    env, headers = _drive_get(monkeypatch, tmp_path)
    assert headers["User-Agent"] in webshape._UA_POOL
    assert env["ua_applied"] == headers["User-Agent"]
    assert env["floor_waited"] == 0.0


def test_caller_ua_wins(monkeypatch, tmp_path):
    env, headers = _drive_get(
        monkeypatch, tmp_path, headers={"User-Agent": "audit-probe/0.9"})
    assert headers["User-Agent"] == "audit-probe/0.9"
    assert env["ua_applied"] == "audit-probe/0.9"


def test_legacy_env_reaches_wire(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_UA", "legacy")
    env, headers = _drive_get(monkeypatch, tmp_path)
    assert headers["User-Agent"] == LEGACY_UA


def test_sticky_is_same_host_same_agent(monkeypatch, tmp_path):
    a, ha = _drive_get(monkeypatch, tmp_path, url="http://example.com/")
    HostFloor._at.clear()
    b, hb = _drive_get(monkeypatch, tmp_path, url="http://example.com/other")
    assert ha["User-Agent"] == hb["User-Agent"]


# --- host floor pacing ----------------------------------------------------------


def test_floor_off_by_default(monkeypatch):
    monkeypatch.delenv("SESSION_HOST_FLOOR", raising=False)
    HostFloor._at.clear()
    assert host_floor_seconds() == 0.0
    assert HostFloor.wait("example.com") == 0.0


def test_floor_paces_second_request(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_HOST_FLOOR", "0.25")
    HostFloor._at.clear()
    _drive_get(monkeypatch, tmp_path, url="http://example.com/")
    env2, _ = _drive_get(monkeypatch, tmp_path, url="http://example.com/next")
    assert env2["floor_waited"] > 0.15
    HostFloor._at.clear()


def test_floor_clamped_to_15(monkeypatch):
    monkeypatch.setenv("SESSION_HOST_FLOOR", "999")
    assert host_floor_seconds() == 15.0


def test_floor_per_host_independent(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_HOST_FLOOR", "0.3")
    HostFloor._at.clear()
    _drive_get(monkeypatch, tmp_path, url="http://example.com/")
    env_b, _ = _drive_get(monkeypatch, tmp_path, url="http://other.net/")
    assert env_b["floor_waited"] == 0.0  # other host: no floor owed
    HostFloor._at.clear()


if __name__ == "__main__":
    passed = failed = 0
    import inspect
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if len(inspect.signature(fn).parameters):
                    print(f"SKIP (needs fixtures): {name}")
                    continue
                fn()
                passed += 1
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed (fixture-dependent tests skipped)")
