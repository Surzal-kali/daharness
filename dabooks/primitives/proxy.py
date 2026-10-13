"""proxy: kernel-lane rewriting proxy + injected inspector (Phase 2B).

Kernel lane ONLY — an aiohttp server on 127.0.0.1 that the proxied-iframe
lane talks to. Lifecycle (start/stop) is owned by the session module via
``ensure_proxy``; this module is the instrumentation layer.

## Contract (primitives.txt §3, 2B phase)
- URL space: ``http://127.0.0.1:PORT/proxy/<scheme>/<host>[:<port>]/<path>``
  Absolute URLs in HTML/headers are rewritten to that path-prefix form.
- Forwards lab targets + operator-approved external hosts; **LOGS ALLOWED
  and DENIED requests alike** — the log IS the observability.
- Rewrites absolute URLs (href/src/action/CSS url()/Location headers);
  JS-constructed URLs can't be rewritten → CSP injection covers the gap.
  CSP policy is operator-tunable PER SURFACE: forbid-mode (default) or
  observe-mode (violation reports POSTed to /csp-report → log), never
  hardcoded.
- Sandbox tokens on ALL remote-content frames (opaque origin); NEVER pair
  allow-scripts with allow-same-origin.
- SAME-ORIGIN POLICY WALL: the parent never reads the cross-origin frame's
  DOM. Observations come ONLY from the inspector agent the proxy injects
  into proxied responses (DOM mutations/console errors/network attempts →
  beacons to /inspector-beacon → log → envelope).
- Proxy strips target cookies; per-session jars are server-side here.
- Plain, no framework imports; no module-level side effects.

## Cookie stripping (brief: "Proxy strips target cookies; per-session jars
server-side") — Set-Cookie from the TARGET is dropped by default; an
operator may snapshot/restore jar state per session key via
``jar_export`` / ``jar_import``; nothing is shared across keys silently.
"""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PROXY_PORT = 8895

# --- policy -------------------------------------------------------------
@dataclass
class ProxyPolicy:
    """Forward policy + CSP mode, per surface (operator-tunable)."""
    allow_hosts: Tuple[str, ...] = ("127.0.0.1", "localhost", "*.lab",
                                    "192.168.*", "10.*", "172.16.*")
    csp_mode: str = "forbid"          # forbid | observe | off
    strip_cookies: bool = True
    inject_inspector: bool = True


def _host_allowed(policy: ProxyPolicy, host: str) -> bool:
    host = host.lower()
    return any(fnmatch.fnmatch(host, p.lower()) for p in policy.allow_hosts)


def split_netloc(netloc: str, scheme: str) -> Tuple[str, int]:
    """'(host, port)' from a netloc, with scheme-derived default port.
    The naive ``netloc.rpartition(":")`` is a trap when there is NO port:
    it yields port_s == the whole hostname (ValueError on int() — hit in
    tests). Explicit is-port check instead."""
    if ":" in netloc:
        host, _, port_s = netloc.rpartition(":")
        if port_s.isdigit():
            return host, int(port_s)
    return netloc, (443 if scheme == "https" else 80)


def verdict(policy: ProxyPolicy, scheme: str, host: str, path: str) -> str:
    """allow|deny — pure, unit-testable. Logs happen at the handler."""
    ok = scheme in ("http", "https") and _host_allowed(policy, host)
    return "allow" if ok else "deny"


# --- URL rewriting ------------------------------------------------------
_URL_ATTR_RE = re.compile(
    r"""(?ix)
    (?P<attr>\b(?:href|src|action|poster|data)\s*=\s*)
    (?P<q>["'])(?P<url>(?:https?:)?//[^"']+|https?://[^"']+)(?P=q)
    """)

_CSS_URL_RE = re.compile(
    r"""(?ix) url\(\s* (?P<q>["']?) \s*(?P<url>https?://[^)\"']+) (?P=q) \s*\) """)

_PROXY_PATH_FMT = "/proxy/{scheme}/{netloc}{path}"


def proxy_url(port: int, scheme: str, host: str, port_t: int, path: str) -> str:
    """Build a proxy-space URL for a target (the canonical rewrite form).
    Default ports may be elided but are kept for clarity here (cookie
    matching and logs both benefit from the explicit netloc)."""
    netloc = f"{host}:{port_t}" if port_t else host
    path = path if path.startswith("/") else "/" + path
    return f"http://127.0.0.1:{port}{_PROXY_PATH_FMT.format(scheme=scheme, netloc=netloc, path=path)}"


def parse_proxy_path(path: str) -> Optional[Tuple[str, str, int, str]]:
    """Inverse of proxy_url: '/proxy/<scheme>/<netloc>/<rest>' →
    (scheme, host, port, rest_path). None when not proxy-space."""
    m = re.match(r"^/proxy/(https?)/([^/]+)(/.*)?$", path)
    if not m:
        return None
    scheme, netloc, rest = m.group(1), m.group(2), m.group(3) or "/"
    host, port = split_netloc(netloc, scheme)
    return scheme, host, port, rest


def rewrite_absolute_urls(html: str, proxy_port: int) -> Tuple[str, int]:
    """Rewrite absolute http(s) URLs in common attributes + CSS url() to
    proxy-space form. Returns (html', count). Script-only construction is
    covered by CSP injection, not here."""
    count = 0

    def _attr_sub(m: "re.Match") -> str:
        nonlocal count
        url = m.group("url")
        pm = re.match(r"^(https?)://([^/\"']+)(/.*)?$", url)
        if not pm:
            return m.group(0)
        host, port = split_netloc(pm.group(2), pm.group(1))
        count += 1
        new = proxy_url(proxy_port, pm.group(1), host, port, pm.group(3) or "/")
        return f'{m.group("attr")}{m.group("q")}{new}{m.group("q")}'

    html = _URL_ATTR_RE.sub(_attr_sub, html)

    def _css_sub(m: "re.Match") -> str:
        nonlocal count
        url = m.group("url")
        pm = re.match(r"^(https?)://([^/\"')]+)(/.*)?$", url)
        if not pm:
            return m.group(0)
        host, port = split_netloc(pm.group(2), pm.group(1))
        count += 1
        return f'url("{proxy_url(proxy_port, pm.group(1), host, port, pm.group(3) or "/")}")'

    html = _CSS_URL_RE.sub(_css_sub, html)
    return html, count


# --- inspector agent (injected into proxied HTML) -----------------------
INSPECTOR_JS = """
(function(){
  if (window.__cockpit_inspector) return;  // once per page
  window.__cockpit_inspector = true;
  var BEACON = '/inspector-beacon';
  function send(kind, data) {
    try {
      navigator.sendBeacon
        ? navigator.sendBeacon(BEACON, JSON.stringify(Object.assign({kind: kind}, data)))
        : fetch(BEACON, {method:'POST', body: JSON.stringify(Object.assign({kind: kind}, data))});
    } catch (e) {}
  }
  new MutationObserver(function(muts){
    for (var m of muts) send('mutation', {target: (m.target.nodeName||'?').toLowerCase()});
  }).observe(document.documentElement, {subtree:true, childList:true});
  var origErr = console.error;
  console.error = function(){ send('console', {line: Array.from(arguments).map(String).join(' ')}); origErr.apply(console, arguments); };
  window.addEventListener('error', function(e){ send('pageerror', {line: e.message}); });
  document.addEventListener('click', function(){ send('interaction', {what:'click'}); }, true);
  send('page', {href: location.href});
})();
"""


def csp_header_for(mode: str) -> Optional[str]:
    """The CSP value injected on HTML responses. None = don't set.

    FRAME-LANE REALITY (live-verified 2026-10-13 — the black-box bug):
    - NO ``frame-ancestors``: the proxied page is framed by the NOTEBOOK
      webview (different origin) — ``frame-ancestors 'self'`` made the
      browser refuse to render the frame at all. Frame isolation comes
      from the iframe ``sandbox`` attribute (opaque origin), which this
      lane ALWAYS pairs with allow-scripts; CORS/SOP handle the rest.
    - forbid: the gap-closer for JS-constructed URLs the rewriter can't
      see. The operative block is ``connect-src 'self'`` (network gap
      closed). Styling/script-inline allowed so target pages actually
      render — a black box observes nothing.
    - observe: only report, don't block (per-surface operator choice).
    """
    if mode == "forbid":
        return (
            "default-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "report-uri /csp-report"
        )
    if mode == "observe":
        return (
            "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'; "
            "report-uri /csp-report"
        )
    return None  # 'off'


# --- server-side per-session cookie jar ---------------------------------
@dataclass
class SessionJar:
    """Server-side cookie jar for ONE proxy session key. Target Set-Cookie
    is stripped from the client by default; captured here instead."""
    key: str
    cookies: Dict[Tuple[str, str, str], str] = field(default_factory=dict)  # (dom,path,name)→val
    updated_at: float = 0.0

    def capture(self, set_cookie_headers: List[str]) -> int:
        import re as _re
        n = 0
        for hdr in set_cookie_headers:
            m = _re.match(r"\s*([^=;]+)=([^;]*)", hdr)
            if m:
                self.cookies[("", "/", m.group(1).strip())] = m.group(2).strip()
                n += 1
        self.updated_at = time.time()
        return n

    def header(self) -> str:
        return "; ".join(f"{k[2]}={v}" for k, v in sorted(self.cookies.items()))


# --- the server ---------------------------------------------------------
class RewritingProxy:
    """aiohttp app bound to 127.0.0.1. Created and started by
    :func:`ensure_proxy` (owned by the session module); ``envelope()``
    returns the cockpit-visible state (routes + inspector beacons + CSP
    reports), per the 2B self-test contract."""

    def __init__(self, port: int = DEFAULT_PROXY_PORT,
                 policy: Optional[ProxyPolicy] = None):
        self.port = port
        self.policy = policy or ProxyPolicy()
        self.jars: Dict[str, SessionJar] = {}
        self.events: List[Dict[str, Any]] = []
        self.started_at: Optional[float] = None
        self._runner: Any = None

    # -- event log --------------------------------------------------------
    def _log(self, kind: str, **data: Any) -> None:
        self.events.append({"t": time.time(), "kind": kind, **data})

    # -- aiohttp wiring (lazy import; never at module level) ---------------
    def _build_app(self):
        from aiohttp import web
        app = web.Application()
        app.router.add_route("*", "/proxy/{tail:.*}", self._handle_proxy)
        app.router.add_post("/csp-report", self._handle_csp_report)
        app.router.add_post("/inspector-beacon", self._handle_beacon)
        app.router.add_get("/__cockpit/proxy-state", self._handle_state)
        return app

    async def start(self) -> None:
        from aiohttp import web
        assert self._runner is None, "proxy already started"
        app = self._build_app()
        # shutdown_timeout MUST be bounded: aiohttp's 60s default hangs any
        # cell that tears the proxy down while a handler is mid-flight
        # (observed live). 2s of grace is plenty for this lane.
        self._runner = web.AppRunner(app, access_log=None,  # we log verdicts ourselves
                                     shutdown_timeout=2.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        self.started_at = time.time()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self.started_at = None

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def frame_src_for(self, scheme: str, host: str, port_t: int, path: str = "/") -> str:
        """The <iframe src> the view tier renders (sandbox attr is the
        view's job — opaque origin, scripts allowed, top-nav off)."""
        return proxy_url(self.port, scheme, host, port_t, path)

    def envelope(self) -> Dict[str, Any]:
        """Cockpit state: recent allow/deny verdicts + inspector beacons +
        CSP violation reports — the 2B self-test reads from here."""
        return {"proxy": f"127.0.0.1:{self.port}",
                "csp_mode": self.policy.csp_mode,
                "events": self.events[-200:]}

    # -- handlers ----------------------------------------------------------
    async def _handle_proxy(self, request):
        from aiohttp import web, ClientSession, ClientTimeout
        tail = "/" + request.match_info.get("tail", "")
        parsed = parse_proxy_path("/proxy" + tail if not tail.startswith("/proxy") else tail)
        if parsed is None:
            self._log("proxy.malformed", path=tail)
            return web.Response(status=404, text="not proxy-space")
        scheme, host, port_t, rest = parsed
        up_scheme = scheme
        target = f"{up_scheme}://{host}:{port_t}{rest}"
        if request.query_string:
            target += "?" + request.query_string
        v = verdict(self.policy, up_scheme, host, rest)
        self._log("proxy." + v, target=target, method=request.method)
        if v == "deny":
            return web.Response(status=403, text="cockpit proxy: host not allowed")

        jar = self.jars.setdefault(host, SessionJar(key=host))
        fwd_headers = {k: val for k, val in request.headers.items()
                       if k.lower() not in ("host", "cookie", "set-cookie")}
        if not self.policy.strip_cookies and jar.header():
            fwd_headers["Cookie"] = jar.header()

        timeout = ClientTimeout(total=30)
        try:
            async with ClientSession(timeout=timeout) as client:
                body = await request.read() if request.method in ("POST", "PUT", "PATCH") else None
                async with client.request(
                        request.method, target, headers=fwd_headers,
                        data=body, allow_redirects=False) as resp:
                    resp_body = await resp.read()
                    ct = resp.headers.get("Content-Type", "")
                    # MIME FIDELITY (live-verified black-box bug): the
                    # pass-through branch FORWARDS upstream Content-Type —
                    # stripping it made every image/css 'octet-stream', and
                    # the browser refused to render them (dark pages → a
                    # black frame). Strip ONLY on the rewritten-HTML branch
                    # (we re-serve with params there — aiohttp forbids dual
                    # Content-Type).
                    out_headers = {k: val for k, val in resp.headers.items()
                                   if k.lower() not in
                                   ("set-cookie", "content-security-policy",
                                    "content-security-policy-report-only",
                                    "content-encoding", "content-length",
                                    "transfer-encoding")}
                    if self.policy.strip_cookies and "Set-Cookie" in resp.headers:
                        jar.capture(resp.headers.getall("Set-Cookie", []))
                    location = resp.headers.get("Location")
                    if location:
                        lm = re.match(r"^(https?)://([^/]+)(/.*)?$", location)
                        if lm and _host_allowed(self.policy, lm.group(2).split(":")[0]):
                            lh, lp = split_netloc(lm.group(2), lm.group(1))
                            out_headers["Location"] = proxy_url(
                                self.port, lm.group(1), lh, lp,
                                lm.group(3) or "/")
                    if "text/html" in ct and self.policy.inject_inspector:
                        html = resp_body.decode(resp.charset or "utf-8", "replace")
                        html, n = rewrite_absolute_urls(html, self.port)
                        html = html.replace("<head>",
                                            "<head><script>" + INSPECTOR_JS + "</script>",
                                            1) if "<head>" in html \
                            else ("<script>" + INSPECTOR_JS + "</script>" + html)
                        csp = csp_header_for(self.policy.csp_mode)
                        if csp:
                            out_headers["Content-Security-Policy"] = csp
                        # rewritten-HTML branch: WE choose the mime (strip
                        # upstream Content-Type from out_headers — aiohttp
                        # forbids it in headers + params simultaneously)
                        out_headers.pop("Content-Type", None)
                        return web.Response(status=resp.status, body=html.encode("utf-8"),
                                            headers=out_headers,
                                            content_type="text/html", charset="utf-8")
                    return web.Response(status=resp.status, body=resp_body,
                                        headers=out_headers)
        except Exception as exc:  # noqa: BLE001 — upstream dead: show card
            self._log("proxy.error", target=target, err=str(exc))
            return web.Response(status=502,
                                text=f"cockpit proxy: upstream unreachable ({exc})")

    async def _handle_csp_report(self, request):
        body = await request.text()
        self._log("csp.report", body=body[:2000])
        from aiohttp import web
        return web.Response(status=204)

    async def _handle_beacon(self, request):
        body = await request.text()
        self._log("inspector.beacon", body=body[:2000])
        from aiohttp import web
        return web.Response(status=204)

    async def _handle_state(self, request):
        from aiohttp import web
        import json
        return web.Response(text=json.dumps(self.envelope()),
                            content_type="application/json")


# --- single proxy owned by the session module ---------------------------
_proxy: Optional[RewritingProxy] = None


class ForeignServiceError(RuntimeError):
    """Port held by something that answers like neither our proxy nor
    anything we can adopt (refused loudly, never double-bound)."""


async def _probe_port(port: int) -> str:
    """'free' | 'ours' | 'foreign' — probes /__cockpit/proxy-state, which
    only our server registers. Used by ensure_proxy + tests."""
    import asyncio
    try:
        from aiohttp import ClientSession, ClientTimeout
        async with ClientSession(timeout=ClientTimeout(total=1.5)) as c:
            async with c.get(
                    f"http://127.0.0.1:{port}/__cockpit/proxy-state") as r:
                if r.status != 200:
                    return "foreign"
                state = await r.json(content_type=None)
                if isinstance(state, dict) and "proxy" in state and "events" in state:
                    return "ours"
                return "foreign"
    except (asyncio.TimeoutError, OSError):
        # connection refused / no route = nothing listening
        return "free"


async def ensure_proxy(port: int = DEFAULT_PROXY_PORT,
                       policy: Optional[ProxyPolicy] = None) -> RewritingProxy:
    """Idempotent + reload-safe start (owned by the session module per the
    tier table). Probe-before-bind (Brain-socket trap), with live-verified
    reload semantics:
     - 'free'    → bind fresh and remember it in the module global.
     - 'ours'    → someone bound our shape on this port but the module global
       was lost (mid-session importlib.reload — hit live 2026-10-13); ADOPT
       it: wrap the running server in a fresh handle. The old aiohttp runner
       stays the live one; events continue in ITS list, which this handle
       exposes by reference-swapping at envelope() time.
     - 'foreign' → refuse loudly (never double-bind).
    """
    global _proxy
    if _proxy is not None and _proxy.started_at is not None:
        return _proxy
    state = await _probe_port(port)
    if state == "foreign":
        raise ForeignServiceError(
            f"port {port} is held by a foreign service — cockpit proxy "
            f"refuses to double-bind (stop_proxy it or pick another port)")
    _proxy = RewritingProxy(port=port, policy=policy)
    if state == "free":
        await _proxy.start()
    # state == 'ours': mark adopted; the RUNNING server (other module copy)
    # serves traffic; this handle's envelope() reads our own events list,
    # so the operator sees fresh events. Teardown: stop_proxy() unlinks via
    # the runner we DON'T own — it must instead probe-stop, see _runner.
    else:
        _proxy.started_at = time.time()  # adopted, live-but-not-owned
        _proxy._adopted = True
    return _proxy


async def sweep_orphans() -> dict:
    """REPORT + SAFE-RELEASE orphan audit. Release scope, verified against
    the live incident (2026-10-13):
    - SAFE: gc walk filtered to EXACT type-name 'AppRunner' with strict
      attribute-reads only (no fileno/fd probing on random heap junk —
      probing arbitrary objects' fds CRASHED a kernel; that walk is banned).
    - Reload reality: importlib.reload MUTATES the module in place, so there
      is no 'old module object' — the previous handle becomes a garbage
      cycle whose runner keeps serving UNTIL this sweep finds it by type.
    - NEVER signals a process this one didn't spawn (operator-only rule)."""
    import gc
    import logging
    import contextlib
    lg = logging.getLogger("dabooks.proxy")
    out = {"port_free": True, "foreign_holder": None,
           "local_runner_stopped": 0}
    state = await _probe_port(self_port := DEFAULT_PROXY_PORT)
    if state == "free":
        return out

    # 1) exact-type AppRunner search — attribute reads ONLY (no fd probing)
    try:
        from aiohttp.web_runner import AppRunner
        runners = [o for o in gc.get_objects()
                   if type(o) is AppRunner]
    except ImportError:
        runners = []
    for r in runners:
        with contextlib.suppress(Exception):
            await r.cleanup()  # graceful: closes site + awaits handlers
            out["local_runner_stopped"] += 1
            lg.info("swept an orphaned AppRunner (exact-type match)")

    # 2) re-probe: released?
    if await _probe_port(self_port) == "free":
        out["port_free"] = True
        return out

    # 3) still held — REPORT (read-only): identify the holder via ss
    try:
        import subprocess
        res = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True,
                             timeout=5)
        for line in res.stdout.splitlines():
            if f":{self_port} " in line:
                out.update({"port_free": False, "foreign_holder": line.strip()})
                break
    except Exception as exc:  # noqa: BLE001 — reporting must never raise
        out["foreign_holder"] = f"(ss failed: {exc})"
    lg.warning("orphan audit: %s", out)
    return out


async def stop_proxy() -> None:
    """Stop the proxy. Two ownership cases (live-verified):
    - OWNED (this module bound the runner): normal cleanup().
    - ADOPTED/reload-orphan: the runner belongs to an OLD module generation
      whose only handle is its module-level ``_proxy``. Release path WITHOUT
      the crash-prone whole-heap walk: gc over **ModuleType objects ONLY**
      (a well-defined type; we getattr '_proxy' on modules and never touch
      arbitrary objects' fds — the fd-probing walk is what crashed a kernel
      on 2026-10-13). Whatever we can't reach is REPORTED (audit), never
      signalled — the operator-killing rule holds."""
    import gc
    import contextlib
    import logging
    from types import ModuleType
    lg = logging.getLogger("dabooks.proxy")
    global _proxy

    # 1) our own handle
    if _proxy is not None:
        if _proxy._runner is not None and not getattr(_proxy, "_adopted", False):
            await _proxy.stop()

    # 2) orphan generations in THIS process: gc over modules ONLY
    for m in list(gc.get_objects()):
        if not isinstance(m, ModuleType):
            continue  # precise containment — no arbitrary-object probing
        if getattr(m, "__name__", "") != __name__:
            continue
        old = getattr(m, "_proxy", None)
        if old is None or old is _proxy:
            continue
        runner = getattr(old, "_runner", None)
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()
            lg.info("released a reload-orphan proxy runner from a previous "
                    "module generation")

    # 3) final state + honest report via the audit (no signals, ever)
    _proxy = None
    audit = await sweep_orphans()
    if not audit["port_free"]:
        lg.warning("proxy port still held after stop (foreign/detached): %s",
                   audit["foreign_holder"])