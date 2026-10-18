"""Tests for web-session fingerprint lacing (evasion/webshape.py wiring).

House conventions (mirrors test_web_session.py):
- the scope gate seam (utils.gated_http.check_scan) is stubbed in-process;
- the cookie jar is isolated to a temp COOKIE_JAR_DB per test;
- gated_request is faked to record the headers actually built.

pytest-compatible AND directly runnable (fixture tests need pytest).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evasion.webshape as webshape
import utils.gated_http as gh
import auxiliaries.web_session as ws
from auxiliaries.web_session import session_get  # noqa: F401 (loopback test)
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
    # Plain path: the session lane always ships a UA in the wire header dict.
    monkeypatch.delenv("SESSION_UA", raising=False)
    monkeypatch.setenv("SESSION_IMPERSONATION", "off")
    HostFloor._at.clear()
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
    HostFloor._at.clear()
    a, ha = _drive_get(monkeypatch, tmp_path, url="http://example.com/")
    HostFloor._at.clear()
    b, hb = _drive_get(monkeypatch, tmp_path, url="http://example.com/other")
    assert a["ua_applied"] == b["ua_applied"]
    assert a["fingerprint"]["target"] == b["fingerprint"]["target"]


# --- identity profiles + impersonation policy (transport-identity stack) ---------

KNOWN_TARGETS = {"chrome136", "chrome145", "safari18_0", "firefox144", "firefox147"}


def test_profile_pool_coherence():
    for p in webshape._PROFILE_POOL:
        assert p["ua"].startswith("Mozilla/5.0 ("), p["ua"]
        if p["family"] == "firefox":
            assert "Firefox/" in p["ua"]
        elif p["family"] == "chrome":
            assert "Chrome/" in p["ua"] and "Firefox/" not in p["ua"]
        elif p["family"] == "safari":
            assert "Safari/" in p["ua"] and not re.search(r"rv:\d", p["ua"])
        if p["target"] == "chrome136":
            applied = dict(p["apply"])
            assert "Windows" in applied["User-Agent"]
            assert applied["sec-ch-ua-platform"] == '"Windows"'
        if webshape.curl_impersonation_available():
            assert p["target"] in KNOWN_TARGETS


def test_sticky_profile_deterministic_per_host():
    a = webshape.sticky_profile("example.com")
    b = webshape.sticky_profile("example.com")
    c = webshape.sticky_profile("other.net")
    assert a["ua"] == b["ua"] and a["target"] == b["target"]
    assert (a["ua"], a["target"]) != (c["ua"], c["target"])


def test_operator_override_profile_has_no_target(monkeypatch):
    monkeypatch.setenv("SESSION_UA", "audit-probe/1.0")
    p = webshape.sticky_profile("example.com")
    assert p["ua"] == "audit-probe/1.0"
    assert p["target"] is None
    monkeypatch.setenv("SESSION_UA", "legacy")
    assert webshape.sticky_profile("example.com")["ua"] == LEGACY_UA


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


# --- impersonation mode + target resolution --------------------------------------


def test_impersonation_mode_env(monkeypatch):
    for off in ("off", "0", "false", "no"):
        monkeypatch.setenv("SESSION_IMPERSONATION", off)
        assert webshape.impersonation_mode() == "off"
    for on in ("", "auto", "yes", "1"):
        monkeypatch.setenv("SESSION_IMPERSONATION", on)
        assert webshape.impersonation_mode() == "auto"


def test_impersonation_target_off_mode(monkeypatch):
    monkeypatch.setenv("SESSION_IMPERSONATION", "off")
    assert webshape.impersonation_target(webshape.sticky_profile("example.com")) is None


def test_impersonation_target_available(monkeypatch):
    monkeypatch.delenv("SESSION_IMPERSONATION", raising=False)
    monkeypatch.delenv("SESSION_UA", raising=False)
    t = webshape.impersonation_target(webshape.sticky_profile("example.com"))
    if webshape.curl_impersonation_available():
        assert t in KNOWN_TARGETS
    else:
        assert t is None


def test_impersonation_target_missing_lib(monkeypatch):
    monkeypatch.delenv("SESSION_IMPERSONATION", raising=False)
    monkeypatch.setattr(webshape, "curl_impersonation_available", lambda: False)
    assert webshape.impersonation_target(webshape.sticky_profile("example.com")) is None


# --- header ordering + fill (plain-path realism) ----------------------------------


def test_order_headers_for_family_chrome():
    ordered = webshape.order_headers_for_family(
        {"x-csrftoken": "t", "User-Agent": "u", "Accept-Encoding": "e", "accept": "a"},
        "chrome",
    )
    keys = [k.lower() for k in ordered]
    assert keys.index("user-agent") < keys.index("accept") < keys.index("accept-encoding")
    assert keys[-1] == "x-csrftoken"  # app header rides the browser set
    assert ordered["User-Agent"] == "u"  # original casing preserved


def test_order_headers_for_family_unknown_family_is_insertion_order():
    keys = list(webshape.order_headers_for_family({"b": "1", "a": "2"}, "unknown"))
    assert keys == ["b", "a"]


def test_canonical_fill_get_fills_trio(monkeypatch):
    monkeypatch.delenv("SESSION_UA", raising=False)
    profile = webshape.sticky_profile("example.com")
    fill = webshape.canonical_fill_for_family(profile, "GET", {"User-Agent": "u"})
    assert set(fill) == {"Accept", "Accept-Language", "Accept-Encoding"}


def test_canonical_fill_post_no_accept(monkeypatch):
    monkeypatch.delenv("SESSION_UA", raising=False)
    profile = webshape.sticky_profile("example.com")
    fill = webshape.canonical_fill_for_family(profile, "POST", {})
    assert "Accept" not in fill
    assert "Accept-Language" in fill and "Accept-Encoding" in fill


def test_canonical_fill_respects_caller_values():
    profile = webshape.sticky_profile("example.com")
    fill = webshape.canonical_fill_for_family(
        profile, "GET", {"User-Agent": "u", "Accept-Language": "de-DE,de;q=0.9"})
    assert "Accept-Language" not in fill  # caller value wins, no duplicate


def test_browser_accept_encoding_only_decodable():
    encs = set(webshape.browser_accept_encoding().split(", "))
    assert {"gzip", "deflate"} <= encs
    assert encs <= {"gzip", "deflate", "br", "zstd"}


# --- transport chooser decisions ---------------------------------------------------


def test_build_session_off_mode(monkeypatch):
    monkeypatch.setenv("SESSION_IMPERSONATION", "off")
    session, fp = ws._build_session(webshape.sticky_profile("example.com"), "GET")
    assert fp["impersonated"] is False and fp["reason"] == "SESSION_IMPERSONATION=off"
    session.close()


def test_build_session_operator_owned(monkeypatch):
    monkeypatch.delenv("SESSION_IMPERSONATION", raising=False)
    profile = dict(webshape.sticky_profile("example.com"), target=None)
    session, fp = ws._build_session(profile, "GET", caller_ua_override=True)
    assert fp["impersonated"] is False
    assert fp["reason"] == "operator-owned UA identity (SESSION_UA/caller override)"
    session.close()


def test_build_session_upload_lane_plain(monkeypatch):
    monkeypatch.delenv("SESSION_IMPERSONATION", raising=False)
    session, fp = ws._build_session(
        webshape.sticky_profile("example.com"), "POST", files_present=True)
    assert fp["impersonated"] is False
    assert "multipart" in fp["reason"]
    session.close()


def test_build_session_impersonated_default(monkeypatch):
    monkeypatch.delenv("SESSION_IMPERSONATION", raising=False)
    monkeypatch.delenv("SESSION_UA", raising=False)
    if not webshape.curl_impersonation_available():
        pytest.skip("curl_cffi not installed")
    profile = webshape.sticky_profile("example.com")
    session, fp = ws._build_session(profile, "GET")
    assert fp["impersonated"] is True
    assert fp["target"] == profile["target"]
    session.close()


# --- envelope fingerprint wiring -----------------------------------------------------


def test_session_get_fingerprint_on_envelope(monkeypatch, tmp_path):
    monkeypatch.delenv("SESSION_IMPERSONATION", raising=False)
    monkeypatch.delenv("SESSION_UA", raising=False)
    HostFloor._at.clear()
    env, headers = _drive_get(monkeypatch, tmp_path)
    fp = env["fingerprint"]
    assert set(fp) >= {"mode", "impersonated", "backend", "target", "reason"}
    if fp["impersonated"]:
        assert fp["backend"] == "curl-impersonate" and fp["target"] in KNOWN_TARGETS
        # UA is template-owned (native pick → absent from caller headers) or
        # the pool's coherent platform override present verbatim
        if "User-Agent" in headers:
            assert headers["User-Agent"] in webshape._UA_POOL
    else:
        assert fp["backend"] == "python-requests" and fp["reason"]


def test_session_get_fingerprint_degraded_off(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_IMPERSONATION", "off")
    monkeypatch.delenv("SESSION_UA", raising=False)
    HostFloor._at.clear()
    env, headers = _drive_get(monkeypatch, tmp_path)
    assert env["fingerprint"]["impersonated"] is False
    assert env["fingerprint"]["backend"] == "python-requests"
    keys = [k.lower() for k in headers]
    assert keys.index("user-agent") < keys.index("accept") < keys.index("accept-encoding")


def test_digest_mentions_fingerprint(monkeypatch, tmp_path):
    monkeypatch.delenv("SESSION_IMPERSONATION", raising=False)
    monkeypatch.delenv("SESSION_UA", raising=False)
    HostFloor._at.clear()
    env, _ = _drive_get(monkeypatch, tmp_path)
    digest = ws._session_digest(env)
    if env["fingerprint"]["impersonated"]:
        assert f"fp: {env['fingerprint']['target']}" in digest["summary"]
    else:
        assert "fp: degraded" in digest["summary"]


# --- real loopback round trip (curl-impersonate client; gate stubbed) ---------------


def test_loopback_impersonated_roundtrip(monkeypatch, tmp_path):
    if not webshape.curl_impersonation_available():
        pytest.skip("curl_cffi not installed")
    monkeypatch.delenv("SESSION_IMPERSONATION", raising=False)
    monkeypatch.delenv("SESSION_UA", raising=False)
    monkeypatch.setenv("COOKIE_JAR_DB", str(tmp_path / "jar.db"))
    monkeypatch.setattr(gh, "check_scan", lambda *a, **k: (True, ""))
    import http.server
    import socketserver
    import threading

    wire: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            wire["ua"] = self.headers.get("User-Agent", "")
            wire["order"] = [k.lower() for k in self.headers.keys()]
            body = b"<html><title>loop</title></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    HostFloor._at.clear()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = session_get(f"http://127.0.0.1:{srv.server_address[1]}/")
    finally:
        srv.shutdown()
    fp = env["fingerprint"]
    assert fp["impersonated"] is True
    assert wire["ua"] == env["ua_applied"]  # envelope claim == wire claim
    assert wire["ua"] in webshape._UA_POOL
    assert wire["order"][0] == "host"  # template-canonical head
    assert "accept" in wire["order"]


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
