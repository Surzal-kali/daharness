"""sessions: kernel-lane browser session module (Phase 1).

Kernel lane ONLY — plain module, ZERO framework imports (the graduation
seam: model-invoked behavior graduates to a framework tool tier by separate
decision; until then this file answers only to human-piloted cells).

## Contract (primitives.txt §3 + §5)
- SessionStore keyed ``host:port``; spawn returns INSTANTLY (the Playwright
  context is created via ``asyncio.create_task`` — never blocks a cell);
  ``await spawn(...)`` resolves once ready.
- Idle reaper = background task on the shared kernel loop (no sleeping
  cells, no threads).
- Every browser context gets route interception installed at spawn with a
  per-session log policy — allowed AND denied requests are both logged; the
  log IS the observability.
- chromium as root REQUIRES --no-sandbox (brief §1; mirrors
  auxiliaries/playwright_sidecar.py). Headless contexts cost ~300-500MB:
  hard cap; spawn fails closed past it with a clean error, never wedges.
- Playwright ASYNC API ONLY (the sync API deadlocks the kernel's loop).

## Browser resolution (evidence-gathered 2026-10-13)
- Repo-local cache ``<WORKSPACE_ROOT>/.pw-browsers/chromium-1243`` EXISTS
  (root's $HOME cache is empty — exactly sidecar's documented trap).
- Operator env may hold a literal ``$WORKSPACE_ROOT`` string (unexpanded
  shell default); sidecar's answer is mirrored below: validate any
  PLAYWRIGHT_BROWSERS_PATH by globbing for chromium*, else fall back to the
  repo-local dir, else let Playwright resolve (driver error surfaces loud).
- System chromium (/usr/bin/chromium) is the last-ditch launch target when
  no playwright bundle is found.

## Purity rules
- No framework imports, no framework records, no SessionManager writes.
- No module-level side effects (Brain scan rule) — bootstrapping happens
  inside :func:`ensure_started`.
- No sync sleeps anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log_name = "dabooks.sessions"

# --- caps / defaults ----------------------------------------------------
import os

MAX_CONTEXTS = int(os.getenv("COCKPIT_MAX_CONTEXTS", "6"))
IDLE_REAP_S = float(os.getenv("COCKPIT_IDLE_REAP_S", "600"))
REAP_INTERVAL_S = float(os.getenv("COCKPIT_REAP_INTERVAL_S", "30"))
_ACTION_TIMEOUT_S = float(os.getenv("COCKPIT_ACTION_TIMEOUT_S", "10"))

_REPO_LOCAL_BROWSERS = ".pw-browsers"


class SessionNotFoundError(KeyError):
    """Raised (fail-closed) when a key isn't in the store."""


class ContextCapError(RuntimeError):
    """Raised when spawning would exceed MAX_CONTEXTS (fail-closed)."""


# --- chromium resolution (mirrors playwright_sidecar._resolve_browsers_path)
def _workspace_root() -> Path:
    v = os.environ.get("WORKSPACE_ROOT", "")
    return Path(v) if v and Path(v).exists() else Path(__file__).resolve().parents[2]


def _has_chromium(parent: str) -> bool:
    import glob
    try:
        return bool(glob.glob(os.path.join(parent, "chromium*")))
    except Exception:
        return False


def resolve_browser() -> Tuple[Optional[str], Dict[str, str]]:
    """Return (executable_path_or_None, env_overrides) for launching.

    Order of preference:
      1. a VALID (checked by glob) PLAYWRIGHT_BROWSERS_PATH in env
      2. WORKSPACE_ROOT / .pw-browsers (repo-local cache, exists today)
      3. system chromium binary (``/usr/bin/chromium``), passed via
         executable_path for direct launch
    Env overrides are applied by the caller right before playwright start.
    """
    env_overrides: Dict[str, str] = {}

    cur = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if cur and _has_chromium(cur):
        return None, {}  # already valid — let playwright resolve in it

    cand = str(_workspace_root() / _REPO_LOCAL_BROWSERS)
    if _has_chromium(cand):
        env_overrides["PLAYWRIGHT_BROWSERS_PATH"] = cand
        return None, env_overrides

    for syspath in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                    "/usr/bin/google-chrome"):
        if Path(syspath).exists():
            return syspath, env_overrides

    return None, env_overrides  # no bundle, no system chrome — loud driver error


# --- policy -------------------------------------------------------------
@dataclass
class LogPolicy:
    """Per-context route policy: allow-by-default with fnmatch DENY rules
    (or the inverse). Both verdicts are logged — observation is constant;
    the verdict is configuration (brief §3)."""
    allow: Tuple[str, ...] = ("*",)
    deny: Tuple[str, ...] = ()
    observe_only: bool = False  # True ⇒ log everything, abort nothing


def default_policy() -> LogPolicy:
    return LogPolicy()


def _matches(url: str, patterns: Tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(url, pat) for pat in patterns)


def verdict(policy: LogPolicy, url: str) -> str:
    """'allow' | 'deny' | 'observe' — pure, unit-testable."""
    if policy.observe_only:
        return "observe"
    if _matches(url, policy.deny) and not _matches(url, policy.allow):
        return "deny"
    if _matches(url, policy.deny) and _matches(url, policy.allow):
        return "deny" if policy.deny else "allow"  # deny wins on overlap
    return "allow"


# --- per-context request logging ----------------------------------------
def make_route_handler(policy: LogPolicy, session_key: str) -> Callable:
    """The interception handler installed on EVERY context at spawn.
    Logs allowed + denied alike with a stable prefix for log scraping."""
    import logging
    lg = logging.getLogger(log_name)

    async def _route(route):
        url = route.request.url
        v = verdict(policy, url)
        if v == "deny":
            lg.info("[%s] DENY  %s", session_key, url)
            try:
                await route.abort()
            except Exception:  # context may be dying; never wedge
                pass
            return
        lg.info("[%s] %s %s", session_key, v.upper(), url)
        try:
            await route.continue_()
        except Exception:
            pass

    return _route


# --- session record -----------------------------------------------------
@dataclass
class Session:
    """One browser context + its metadata. The Playwright objects are
    opaque to the cockpit (created inside the spawn task)."""
    key: str                    # "host:port"
    created_at: float
    last_used: float
    policy: LogPolicy
    route_handler: Any = None
    context: Any = None         # playwright BrowserContext when spawned
    page: Any = None            # first page in the context
    ready: Any = None           # Future/task handle for readiness await
    status: str = "spawning"    # spawning|ready|dead|reaped|killed
    error: Optional[str] = None
    shots: int = 0
    origin: str = ""            # "http://host:port" base URL (or proxy-space when via_proxy)
    via_proxy: bool = False     # routed through the rewriting proxy (proxied recon)

    def touch(self) -> None:
        self.last_used = time.monotonic()


# --- the store ----------------------------------------------------------
class SessionStore:
    """Keyed host:port → Session. Owns spawn/kill + the idle reaper task.

    The store holds NO playwright import at module level — playwright is
    imported lazily inside ``ensure_started`` so import stays cheap and
    unit tests can inject fakes via ``store._pw`` / ``store._browser``.
    """

    def __init__(self, max_contexts: int = MAX_CONTEXTS,
                 idle_reap_s: float = IDLE_REAP_S,
                 reap_interval_s: float = REAP_INTERVAL_S,
                 action_timeout_s: float = _ACTION_TIMEOUT_S):
        self._sessions: Dict[str, Session] = {}
        self.max_contexts = max_contexts
        self.idle_reap_s = idle_reap_s
        self.reap_interval_s = reap_interval_s
        self.action_timeout_s = action_timeout_s
        self._pw = None          # playwright handle (or test fake)
        self._browser = None     # launched browser (or test fake)
        self._reaper: Optional[asyncio.Task] = None
        self._env_overrides: Dict[str, str] = {}

    # -- introspection ----------------------------------------------------
    def keys(self) -> List[str]:
        return sorted(self._sessions)

    def get(self, key: str) -> Session:
        """Fail-closed lookup: raises SessionNotFoundError (never None).
        Marks the session used (reaper timer refresh)."""
        sn = self._sessions.get(key)
        if sn is None:
            raise SessionNotFoundError(key)
        sn.touch()
        return sn

    def count(self) -> int:
        return len(self._sessions)

    def find_by_host(self, host: str, port: int) -> str:
        """The canonical key form."""
        return f"{host}:{port}"

    # -- spawn ------------------------------------------------------------
    async def spawn(self, host: str, port: int, policy: Optional[LogPolicy] = None,
                    via_proxy: bool = False) -> Session:
        """Spawn a browser context for host:port. Returns INSTANTLY with a
        session whose ``status`` starts at 'spawning'; the real context
        creation proceeds as a task on the shared loop. ``await`` on the
        session's ``ready`` future (or :func:`wait_ready`) to block until
        navigable. Past MAX_CONTEXTS ⇒ ContextCapError (fail-closed, with
        the keys of what's holding the slots in the message).

        ``via_proxy=True`` routes navigation through the lane's rewriting
        proxy (started in-process via proxy.ensure_proxy) — the proxied-
        recon path: full instrumentation (URL rewriting, inspector
        injection, CSP enforcement, verdict/beacon logs) consumed through
        the VNC screenshot lane (the render that works headless — the
        proxied-IFRAME view tier was cut on 2026-10-13: no operator eye
        rides a headless VM's host loopback)."""
        import logging
        lg = logging.getLogger(log_name)
        key = self.find_by_host(host, port)
        existing = self._sessions.get(key)
        if existing is not None and existing.status in ("spawning", "ready"):
            existing.touch()
            return existing  # idempotent spawn

        if self.count() >= self.max_contexts:
            raise ContextCapError(
                f"context cap reached ({self.max_contexts}); live: {self.keys()}")

        origin = f"http://{host}:{port}"
        if via_proxy:
            # in-lane dependency: the proxy module owns its port + policy;
            # pass THIS session's policy through so the verdict log reflects
            # the same rules the context's route handler applies.
            from dabooks.primitives import proxy as _proxy_mod
            pr = await _proxy_mod.ensure_proxy(policy=_proxy_mod.ProxyPolicy(
                allow_hosts=(host,), csp_mode="forbid"))
            origin = pr.frame_src_for("http", host, port, "/")

        sn = Session(key=key, created_at=time.monotonic(),
                     last_used=time.monotonic(),
                     policy=policy or default_policy(),
                     origin=origin)
        sn.via_proxy = via_proxy
        self._sessions[key] = sn
        lg.info("spawn requested: %s (via_proxy=%s)", key, via_proxy)
        sn.ready = asyncio.create_task(self._spawn_task(sn))
        return sn

    async def wait_ready(self, host: str, port: int, timeout_s: float = 30.0) -> Session:
        """Await a spawn's readiness (its navigation to the origin root).
        Timeout ⇒ Session.status='dead' + error, raise SessionNotFoundError
        is NOT used here — the session stays visible with an error attached."""
        key = self.find_by_host(host, port)
        sn = self._sessions.get(key)
        if sn is None:
            raise SessionNotFoundError(key)
        try:
            if isinstance(sn.ready, asyncio.Task):
                await asyncio.wait_for(asyncio.shield(sn.ready), timeout=timeout_s)
        except asyncio.TimeoutError:
            sn.status = "dead"
            sn.error = f"spawn timeout after {timeout_s}s"
        return sn

    async def _spawn_task(self, sn: Session) -> None:
        """The real spawn — happens on the loop without blocking a cell.
        Import + launch + context + interception wiring all live here so
        failure marks the session dead with the reason in ``sn.error``."""
        import logging
        lg = logging.getLogger(log_name)
        try:
            if self._browser is None:
                from playwright.async_api import async_playwright
                exe, self._env_overrides = resolve_browser()
                for k, v in self._env_overrides.items():
                    os.environ[k] = v
                if self._pw is None:
                    self._pw = await async_playwright().start()
                args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                launch_kwargs: Dict[str, Any] = {"headless": True, "args": args}
                if exe:
                    launch_kwargs["executable_path"] = exe
                self._browser = await self._pw.chromium.launch(**launch_kwargs)
            context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                device_scale_factor=1,  # screenshots are 1:1 with viewport
                # coords — widget→viewport math in views.py needs NO guessing
            )
            handler = make_route_handler(sn.policy, sn.key)
            await context.route("**/*", handler)
            page = await context.new_page()
            await page.goto(sn.origin, wait_until="domcontentloaded", timeout=25_000)
            sn.context, sn.page, sn.route_handler = context, page, handler
            sn.status = "ready"
            lg.info("spawned: %s status=ready", sn.key)
        except Exception as exc:  # noqa: BLE001 — mark dead, never wedge
            sn.status = "dead"
            sn.error = f"{type(exc).__name__}: {exc}"
            lg.warning("spawn failed for %s: %s", sn.key, sn.error)
            # release the browser if the launch itself was the failure
            if sn.context is None and self._browser is not None:
                with contextlib.suppress(Exception):
                    await self._browser.close()
                self._browser = None

    # -- kill / reap ------------------------------------------------------
    async def kill(self, host: str, port: int) -> str:
        """Close a session's context and mark it killed. Fail-closed:
        unknown key ⇒ SessionNotFoundError. Idempotent for already-killed/
        dead/reaped sessions (returns current status, no double close).
        Never raises on close errors — the session is marked dead either way."""
        import logging
        lg = logging.getLogger(log_name)
        key = self.find_by_host(host, port)
        sn = self._sessions.get(key)
        if sn is None:
            raise SessionNotFoundError(key)
        sn.touch()
        if sn.status in ("killed", "dead", "reaped"):
            return sn.status  # already down — idempotent
        if sn.context is not None:
            with contextlib.suppress(Exception):
                await sn.context.close()
        sn.status = "killed"
        lg.info("killed: %s", key)
        return sn.status

    async def reap_idle(self) -> List[str]:
        """Close sessions idle beyond the threshold. Returns the reaped keys.
        Called by the reaper task; also callable defensively from a cell."""
        cutoff = time.monotonic() - self.idle_reap_s
        reaped: List[str] = []
        for key, sn in list(self._sessions.items()):
            if sn.status == "ready" and sn.last_used < cutoff:
                host, _, port = key.rpartition(":")
                await self.kill(host, int(port))
                reaped.append(key)
        return reaped

    def start_reaper(self) -> asyncio.Task:
        """Start the idle reaper as a background task on the shared loop
        (no sleeping cells — the sleep lives in the task, per brief §5)."""
        if self._reaper is not None and not self._reaper.done():
            return self._reaper
        import logging
        lg = logging.getLogger(log_name)

        async def _loop():
            while True:
                try:
                    await asyncio.sleep(self.reap_interval_s)
                    for key in await self.reap_idle():
                        lg.info("reaped idle: %s", key)
                except asyncio.CancelledError:
                    return
                except Exception as exc:  # noqa: BLE001 — reaper never dies
                    lg.warning("reaper tick failed: %s", exc)

        self._reaper = asyncio.create_task(_loop())
        return self._reaper

    async def stop_reaper(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None

    async def kill_all(self) -> List[str]:
        """Kill every live (ready/spawning) session; returns keys actually
        killed this call (already-killed sessions are skipped, idempotently)."""
        killed: List[str] = []
        for key in [k for k, sn in self._sessions.items()
                    if sn.status in ("ready", "spawning")]:
            host, _, port = key.rpartition(":")
            await self.kill(host, int(port))
            killed.append(key)
        return killed

    # -- screenshots ------------------------------------------------------
    def _lookup(self, key: str) -> Session:
        """Internal non-touching lookup (reaper/kill paths already touch)."""
        sn = self._sessions.get(key)
        if sn is None:
            raise SessionNotFoundError(key)
        return sn

    async def screenshot(self, host: str, port: int, full: bool = False) -> bytes:
        """PNG bytes of the session's current viewport. Fail-closed on a
        dead session with its stored error, never a raw traceback."""
        sn = self._sessions.get(self.find_by_host(host, port))
        if sn is None:
            raise SessionNotFoundError(self.find_by_host(host, port))
        sn.touch()
        if sn.status != "ready" or sn.page is None:
            raise RuntimeError(f"session {sn.key} not ready "
                               f"(status={sn.status}, error={sn.error})")
        try:
            png = await asyncio.wait_for(sn.page.screenshot(full_page=full),
                                         timeout=self.action_timeout_s)
        except asyncio.TimeoutError:
            raise RuntimeError(f"session {sn.key} screenshot timed out "
                               f"after {self.action_timeout_s}s")
        sn.shots += 1
        return png

    # -- events -----------------------------------------------------------
    async def click(self, host: str, port: int, x: int, y: int) -> None:
        sn = self._sessions.get(self.find_by_host(host, port))
        if sn is None:
            raise SessionNotFoundError(self.find_by_host(host, port))
        sn.touch()
        if sn.status != "ready" or sn.page is None:
            raise RuntimeError(f"session {sn.key} not ready "
                               f"(status={sn.status}, error={sn.error})")
        await self._bounded(sn.page.mouse.click(x, y), sn)

    async def press(self, host: str, port: int, key: str) -> None:
        sn = self._sessions.get(self.find_by_host(host, port))
        if sn is None:
            raise SessionNotFoundError(self.find_by_host(host, port))
        sn.touch()
        if sn.status != "ready" or sn.page is None:
            raise RuntimeError(f"session {sn.key} not ready "
                               f"(status={sn.status}, error={sn.error})")
        await self._bounded(sn.page.keyboard.press(key), sn)

    async def raw(self, host: str, port: int, fn) -> None:
        """Run a caller-supplied async lambda bound to the page, under the
        same action bound. Escape hatch for button/mouse-button/drag
        primitives the view needs without leaking page internals upwards:
        ``await store.raw(h, p, lambda page: page.mouse.down())``."""
        sn = self._sessions.get(self.find_by_host(host, port))
        if sn is None:
            raise SessionNotFoundError(self.find_by_host(host, port))
        sn.touch()
        if sn.status != "ready" or sn.page is None:
            raise RuntimeError(f"session {sn.key} not ready "
                               f"(status={sn.status}, error={sn.error})")
        await self._bounded(fn(sn.page), sn)

    # -- banner probes (Phase 3 contract: cards lane target-IO) -----------
    SILENT_PROTOCOLS = {21, 22, 25, 587, 139, 445, 3306, 5432}  # wait-for-client-hello ports → read may be empty

    async def banner_probe(self, port: int, host: Optional[str] = None,
                           timeout: float = 5.0) -> str:
        """Raw banner grab: connect, read first bytes. Bounded + fail-closed
        (timeout/blocked ⇒ '' with a note via the caller's card rendering).
        Silent-by-protocol ports (smb waits for a client hello, etc.) are
        EXPECTED to read ∅ — reported as empty, never an error (observed
        live on OWASP-BWA: ssh+imap greet, netbios silent)."""
        import asyncio
        host = host or self._last_host_for_port(port) or ""
        if not host:
            return ""
        probe_id = f"banner:{host}:{port}"
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout)
        except (asyncio.TimeoutError, OSError) as exc:
            import logging
            logging.getLogger(log_name).info(
                "[%s] banner refused: %s: %s", probe_id,
                type(exc).__name__, exc)
            return ""
        try:
            data = await asyncio.wait_for(reader.read(256), timeout=2.0)
        except asyncio.TimeoutError:
            import logging
            logging.getLogger(log_name).info(
                "[%s] banner silent (protocol waits for client hello)", probe_id)
            data = b""
        finally:
            with contextlib.suppress(Exception):
                writer.close()
        return data.decode("utf-8", "replace").strip()

    def _last_host_for_port(self, port: int) -> Optional[str]:
        """Most recent session host for this port (card probes ride the
        sweep's discovered target)."""
        import time as _t
        best, best_t = None, -1
        for key, sn in self._sessions.items():
            h, _, p = key.rpartition(":")
            if p == str(port) and sn.last_used > best_t:
                best, best_t = h, sn.last_used
        return best

    async def _bounded(self, coro, sn: Session) -> None:
        """Action waits are bounded: timeout ⇒ RuntimeError naming the
        session (fail-closed per brief §2), never a wedged loop. Every
        pointer/key interaction and screenshot honors the same knob."""
        try:
            return await asyncio.wait_for(coro, timeout=self.action_timeout_s)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"action on {sn.key} timed out after {self.action_timeout_s}s")


# --- module-level singleton --------------------------------------------
_store: Optional[SessionStore] = None


def get_store() -> SessionStore:
    """The cockpit's session store. One per kernel process (this is the
    kernel lane; no cross-process semantics are attempted)."""
    global _store
    if _store is None:
        _store = SessionStore()
    return _store


async def ensure_started() -> SessionStore:
    """Idempotent boot: get_store + launch the reaper (browser launches
    lazily on first spawn)."""
    st = get_store()
    st.start_reaper()
    return st