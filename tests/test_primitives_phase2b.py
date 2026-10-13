"""Phase-2B tests: rewriting proxy, CSP modes, inspector injection,
cookie stripping/jar + the via_proxy VNC-lane path (the proxied-IFRAME
RENDER tier was cut 2026-10-13 — headless VMs have no webview eye on the
host loopback; the proxy's value is INSTRUMENTATION behind the session
lane, not a view).

Unit tests are pure; the integration test runs the REAL aiohttp proxy on
loopback against a local fixture (no external egress).
"""

import asyncio
import json

import pytest

from dabooks.primitives.proxy import (
    ProxyPolicy, RewritingProxy, SessionJar, csp_header_for,
    parse_proxy_path, proxy_url, rewrite_absolute_urls, verdict,
)


def _run(coro):
    return asyncio.run(coro)


# --- URL rewriting (pure) ------------------------------------------------
def test_rewrite_href_and_src_and_action():
    html = '<a href="http://lab.local/admin/">x</a>' \
           '<img src="http://lab.local/logo.png">' \
           '<form action="https://lab.local:8443/login">'
    out, n = rewrite_absolute_urls(html, 8895)
    assert n == 3
    assert 'href="http://127.0.0.1:8895/proxy/http/lab.local:80/admin/"' in out
    assert 'src="http://127.0.0.1:8895/proxy/http/lab.local:80/logo.png"' in out
    assert 'action="http://127.0.0.1:8895/proxy/https/lab.local:8443/login"' in out


def test_rewrite_css_urls():
    html = "<style>body{background:url(http://lab.local/bg.png)}</style>"
    out, n = rewrite_absolute_urls(html, 8895)
    assert n == 1
    assert 'url("http://127.0.0.1:8895/proxy/http/lab.local:80/bg.png")' in out


def test_rewrite_leaves_relative_and_protocol_relative_alone_when_unparseable():
    out, n = rewrite_absolute_urls('<a href="/local/x">l</a>', 8895)
    assert n == 0 and 'href="/local/x"' in out


def test_proxy_url_and_parse_roundtrip():
    u = proxy_url(8895, "https", "lab.local", 8443, "/a/b")
    assert u == "http://127.0.0.1:8895/proxy/https/lab.local:8443/a/b"
    # query is NOT part of the rewritten attr — it rides the request
    parsed = parse_proxy_path("/proxy/https/lab.local:8443/a/b")
    assert parsed == ("https", "lab.local", 8443, "/a/b")


def test_parse_proxy_path_default_ports():
    assert parse_proxy_path("/proxy/http/lab.local/x") == \
        ("http", "lab.local", 80, "/x")
    assert parse_proxy_path("/proxy/https/lab.local/") == \
        ("https", "lab.local", 443, "/")


def test_parse_proxy_path_non_proxy_space():
    assert parse_proxy_path("/normal/path") is None


# --- policy verdict (pure) ----------------------------------------------
def test_verdict_allows_lab_and_denies_unknown():
    p = ProxyPolicy(allow_hosts=("127.0.0.1", "*.lab", "192.168.*"))
    assert verdict(p, "http", "foo.lab", "/") == "allow"      # wildcard subdomain
    assert verdict(p, "http", "192.168.5.5", "/") == "allow"  # IP glob
    assert verdict(p, "http", "127.0.0.1", "/") == "allow"
    assert verdict(p, "https", "evil.example.com", "/") == "deny"


def test_verdict_exact_hostmatch():
    p = ProxyPolicy(allow_hosts=("lab.local",))
    assert verdict(p, "http", "lab.local", "/") == "allow"
    assert verdict(p, "http", "other.local", "/") == "deny"


def test_csp_forbid_blocks_connect():
    csp = csp_header_for("forbid")
    assert "connect-src 'self'" in csp
    assert "report-uri /csp-report" in csp


def test_csp_observe_only_reports():
    csp = csp_header_for("observe")
    assert "unsafe-inline" in csp  # permissive-but-observed
    assert "report-uri /csp-report" in csp


def test_csp_off_is_none():
    assert csp_header_for("off") is None


# --- inspector injection -------------------------------------------------
def test_inspector_js_is_injectable_and_idempotent_flagged():
    from dabooks.primitives.proxy import INSPECTOR_JS
    assert "__cockpit_inspector" in INSPECTOR_JS  # once-per-page guard
    assert "/inspector-beacon" in INSPECTOR_JS


# --- session jar ---------------------------------------------------------
def test_jar_captures_set_cookie_and_headers():
    jar = SessionJar(key="lab.local")
    n = jar.capture(["a=1; Path=/; HttpOnly", "b=2"])
    assert n == 2
    assert jar.header() == "a=1; b=2"


# --- the cut render tier's sandbox contract lives on in the module docs --
# (ProxiedView/proxied_frame removed; the sandbox tokens rule survives in
# proxy.py's docs + the git history of views.py)


# --- REAL loopback integration (fixture behind the proxy) ---------------
# CRITICAL TEST-HARNESS RULE (learned by wedging a terminal): the fixture
# server, the proxy, AND the client fetches must live on ONE event loop.
# Starting aiohttp with an isolated asyncio.run() in a pytest fixture closes
# that loop on return — the server's tasks die while the socket stays
# bound, and the client fetch hangs into an unserved backlog (the kernel
# lane never hits this: its loop lives for the whole session).
# So: each integration test = one coroutine run via asyncio.run, with
# everything inside, plus HARD ClientTimeouts so pytest can never hang.


async def _start_fixture(host: str, port: int, beacon_hits: list):
    """Start the lab fixture server; returns its runner (stop via runner.cleanup)."""
    from aiohttp import web

    async def page(request):
        return web.Response(text="""
            <html><head><title>fixture</title></head><body>
            <a href="http://{host}/inner">inner</a>
            <img src="http://{host}/pixel.png">
            <script>fetch('http://{host}/tracker?leak=1')</script>
            </body></html>""".replace("{host}", request.host),
            content_type="text/html")

    async def inner(request):
        return web.Response(text="<html><body>inner ok</body></html>",
                            content_type="text/html")

    async def tracker(request):
        beacon_hits.append(dict(request.query))
        return web.Response(status=204, headers={"Set-Cookie": "sid=xyz"})

    async def pixel(request):
        return web.Response(body=b"\x89PNG", content_type="image/png")

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/inner", inner)
    app.router.add_get("/tracker", tracker)
    app.router.add_get("/pixel.png", pixel)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


def _fetch(base: str, timeout_s: float = 5.0):
    """Bounded GET returning (status, text, headers-dict)."""
    from aiohttp import ClientSession, ClientTimeout

    async def go():
        async with ClientSession(timeout=ClientTimeout(total=timeout_s)) as c:
            async with c.get(base) as r:
                return r.status, await r.text(), dict(r.headers)
    return go


def test_integration_rewrite_inspect_allow_deny():
    async def run():
        from aiohttp import ClientSession, ClientTimeout
        beacon_hits: list = []
        host, port = "127.0.0.1", 8894
        fixture_runner = await _start_fixture(host, port, beacon_hits)
        pr = None
        try:
            pr = RewritingProxy(port=8896, policy=ProxyPolicy(
                allow_hosts=("127.0.0.1",), csp_mode="forbid"))
            await pr.start()
            async with ClientSession(timeout=ClientTimeout(total=5)) as c:
                async with c.get(pr.frame_src_for("http", host, port, "/")) as r:
                    assert r.status == 200
                    html = await r.text()
                    assert r.headers.get("Content-Security-Policy")
                    assert f"/proxy/http/{host}:{port}/inner" in html  # rewrite
                    assert "__cockpit_inspector" in html              # injection
                async with c.get(pr.frame_src_for(
                        "http", "evil.example.com", 80, "/")) as r:
                    assert r.status == 403                             # deny
            env = pr.envelope()
            kinds = [e["kind"] for e in env["events"]]
            assert "proxy.allow" in kinds
            assert "proxy.deny" in kinds
        finally:
            if pr is not None:
                await pr.stop()
            await fixture_runner.cleanup()
    _run(run())


def test_integration_beacon_endpoints_log():
    """The 2B SELF-TEST contract: beacon-fired events show in the log
    regardless of verdict (beacon endpoints accept, log, 204)."""
    async def run():
        from aiohttp import ClientSession, ClientTimeout
        pr = RewritingProxy(port=8897, policy=ProxyPolicy(
            allow_hosts=("127.0.0.1",), csp_mode="forbid"))
        await pr.start()
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as c:
                await c.post("http://127.0.0.1:8897/inspector-beacon",
                             data=json.dumps({"kind": "page", "href": "/x"}))
                await c.post("http://127.0.0.1:8897/csp-report",
                             data=json.dumps({"csp-report": {"t": 1}}))
            env = pr.envelope()
            kinds = [e["kind"] for e in env["events"]]
            assert "inspector.beacon" in kinds
            assert "csp.report" in kinds
        finally:
            await pr.stop()
    _run(run())


def test_integration_cookie_strip_and_jar_capture():
    async def run():
        from aiohttp import ClientSession, ClientTimeout
        beacon_hits: list = []
        host, port = "127.0.0.1", 8894
        fixture_runner = await _start_fixture(host, port, beacon_hits)
        pr = None
        try:
            pr = RewritingProxy(port=8898, policy=ProxyPolicy(
                allow_hosts=("127.0.0.1",), csp_mode="forbid",
                strip_cookies=True))
            await pr.start()
            async with ClientSession(timeout=ClientTimeout(total=5)) as c:
                async with c.get(pr.frame_src_for(
                        "http", host, port, "/tracker")) as r:
                    assert r.status == 204
                    assert "Set-Cookie" not in r.headers  # STRIPPED client-side
            assert pr.jars[host].header() == "sid=xyz"  # captured server-side
        finally:
            if pr is not None:
                await pr.stop()
            await fixture_runner.cleanup()
    _run(run())


def test_via_proxy_session_routes_through_proxy():
    """The SUCCESSOR to the cut view tier: sessions.spawn(via_proxy=True)
    navigates the chromium session through the rewriting proxy — full
    instrumentation, VNC-lane rendering. Starts its own fixture via the
    same one-loop helper (the fixture_site fixture variant was removed
    with the render tier)."""
    async def run():
        from dabooks.primitives.proxy import ensure_proxy, stop_proxy
        from dabooks.primitives.sessions import SessionStore
        from unittest.mock import MagicMock, patch
        # fake browser page records its final URL (the proxy-space origin)
        captured = {}
        class P:
            async def goto(self, url, **k): captured["url"] = url
        class C:
            async def route(self, *a): pass
            async def new_page(self): return P()
        class B:
            async def new_context(self, **k): return C()
        st = SessionStore()
        st._browser = B()
        pr = await ensure_proxy(port=8896, policy=ProxyPolicy(allow_hosts=("127.0.0.1",)))
        try:
            sn = await st.spawn("127.0.0.1", 8894, via_proxy=True)
            await sn.ready
            assert sn.via_proxy is True
            assert "/proxy/http/127.0.0.1:8894" in captured["url"]
            # NOTE: the fake goto records the URL but doesn't fetch, so the
            # envelope-events contract is covered by the REAL integration
            # test below (test_integration_rewrite_inspect_allow_deny +
            # the live-kernel run) — not asserted here.
        finally:
            await stop_proxy()
            await st.kill_all()
            await st.stop_reaper()
    _run(run())


# --- purity --------------------------------------------------------------
def test_proxy_module_no_framework_imports():
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "dabooks" / "primitives"
           / "proxy.py").read_text()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                if n.split(".")[0] in ("daharness", "constants", "utils", "auxiliaries"):
                    offenders.append(n)
    assert offenders == [], offenders