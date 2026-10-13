"""views: kernel-lane VNC view + action relay (Phase 2).

Kernel lane ONLY — renders screenshots, relays events, and NEVER writes
framework records (brief §2 render tier). Playwright objects are touched
exclusively through the Phase-1 session store.

## Contract (primitives.txt §2/§3/§5)
- anywidget (inline JS/CSS assets only — no build step a kernel restart
  could orphan): render last PNG; forward click/drag/keys/scroll.
- widget-pixels → viewport scaled, MIND SCREENSHOT SCALE FACTOR: Phase 1
  pins deviceScaleFactor=1 with a 1280x800 viewport, so the mapping is a
  plain ratio and exact.
- Action timeout fails CLOSED with an error card, never wedges the loop.
- Status line alive/dead; dead = last screenshot + "session ended" card.
- Plain-mode fallback: ``render("plain")`` degrades to a text card when
  widget assets are unavailable/failed.
- Interactivity via the comm channel (traitlets sync), NEVER stdin.

## Event protocol (comm direction: widget → kernel)
``{"type": "click"|"mousedown"|"mouseup"|"mousemove"|"wheel"|"key",
   "x"?: int, "y"?: int, "dx"?: int, "dy"?: int,
   "key"?: str, "modifiers"?: [str]}``
The kernel replies by updating ``error`` / ``ready_`` traits; the widget
renders the error card when ``error`` is non-empty.
"""

from __future__ import annotations

import anywidget as anywidget_mod
import traitlets as traitlets_mod

import base64
import time
from typing import Any, Dict, Optional

VIEWPORT_W, VIEWPORT_H = 1280, 800   # viewport is pinned at spawn (Phase 1)
DEFAULT_VIEW_W, DEFAULT_VIEW_H = 960, 600   # on-screen render size (CSS px)
_REFRESH_TIMEOUT_S = 8.0  # screenshot bound; fail-closed card past it

_WIDGET_CSS = """
.vnc-shell { font-family: ui-monospace, monospace; }
.vnc-status { padding: 4px 8px; background: #222; color: #9f9;
              font-size: 12px; border-radius: 4px 4px 0 0; }
.vnc-status.dead { color: #f99; }
.vnc-status.boot { color: #ff9; }  /* 'booting…' reads as progress, not error
   (the '@jupyter-widgets/base' loading message is the FRONTEND's own
   timeout during first render — slow load ≠ broken widget; measured on
   kali, 2026-10-13) */
.vnc-status .err { color: #f99; }
.vnc-canvas { border-radius: 0 0 4px 4px; cursor: crosshair;
              image-rendering: pixelated; outline: none; }
.vnc-card { padding: 10px; border: 1px solid #f99; color: #f99;
            font-family: ui-monospace, monospace; }
"""

_WIDGET_JS = """
function render({ model, el }) {
  el.classList.add("vnc-shell");
  const state = document.createElement("div");
  const canvas = document.createElement("img");
  canvas.className = "vnc-canvas";
  canvas.tabIndex = 0;
  const card = document.createElement("div");

  const draw = () => {
    const b64 = model.get("png_b64");
    if (b64) { canvas.src = "data:image/png;base64," + b64; canvas.style.width = model.get("view_width") + "px"; canvas.style.height = model.get("view_height") + "px"; }
    const st = model.get("status");
    const err = model.get("error");
    state.className = "vnc-status" +
      (st === "dead" || st === "killed" ? " dead"
     : st === "boot" || st === "booting" ? " boot" : "");
    state.textContent = (err ? err + " · " : "") + st;
    if (st === "dead" || st === "killed") {
      card.className = "vnc-card";
      card.textContent = "session ended — showing last screenshot (re-spawn to re-attach)";
    } else if (err) {
      card.className = "vnc-card";
      card.textContent = err;
    } else { card.textContent = ""; card.className = ""; }
  };
  model.on("change:png_b64", draw);
  model.on("change:status", draw);
  model.on("change:error", draw);

  const send = (type, extra) => {
    // anywidget COMM CONTRACT: set + save_changes — without save_changes the
    // trait change stays browser-local and NEVER reaches the kernel (the
    // 'widget is just an image' bug: view rendered, nothing relayed).
    model.set("event", Object.assign({ type }, extra));
    model.save_changes();
  };
  const scale = () => {
    const w = model.get("view_width") || canvas.naturalWidth || 1280;
    const h = model.get("view_height") || canvas.naturalHeight || 800;
    const r = canvas.getBoundingClientRect();
    return { sx: (model.get("viewport_w") || 1280) / r.width,
             sy: (model.get("viewport_h") || 800) / r.height };
  };
  const evpos = (e) => {
    const s = scale(); const r = canvas.getBoundingClientRect();
    return { x: Math.round((e.clientX - r.left) * s.sx),
             y: Math.round((e.clientY - r.top) * s.sy) };
  };
  canvas.addEventListener("mousedown", (e) => { canvas.focus(); send("mousedown", evpos(e)); });
  canvas.addEventListener("mouseup",   (e) => send("mouseup", evpos(e)));
  canvas.addEventListener("mousemove", (e) => { if (e.buttons) send("drag", evpos(e)); });
  canvas.addEventListener("click",     (e) => send("click", evpos(e)));
  canvas.addEventListener("wheel",     (e) => { e.preventDefault();
      send("wheel", { dx: e.deltaX, dy: e.deltaY }); }, { passive: false });
  canvas.addEventListener("keydown",   (e) => {
      e.preventDefault();
      const modifiers = [];
      if (e.ctrlKey) modifiers.push("Control"); if (e.altKey) modifiers.push("Alt");
      if (e.shiftKey) modifiers.push("Shift");  if (e.metaKey) modifiers.push("Meta");
      send("key", { key: e.key, modifiers }); });
  model.on("change:error", () => { /* error card drawn by draw() */ });

  el.appendChild(state); el.appendChild(canvas); el.appendChild(card);
  draw();
}
export default { render };
"""


class _CockpitWidget(anywidget_mod.AnyWidget):
    """The anywidget for one VNC view. Defined ONCE at module level — a
    class-body default like ``Int(view_width)`` would make the name
    class-local and raise NameError (class scopes don't fall back to the
    enclosing scope for assigned names; hit live, tested in phase-2).
    Per-instance values flow through the constructor kwargs instead."""
    _esm = _WIDGET_JS
    _css = _WIDGET_CSS
    png_b64 = traitlets_mod.Unicode("").tag(sync=True)
    status = traitlets_mod.Unicode("boot").tag(sync=True)
    error = traitlets_mod.Unicode("").tag(sync=True)
    event = traitlets_mod.Dict({}).tag(sync=True)
    view_width = traitlets_mod.Int(DEFAULT_VIEW_W).tag(sync=True)
    view_height = traitlets_mod.Int(DEFAULT_VIEW_H).tag(sync=True)
    viewport_w = traitlets_mod.Int(VIEWPORT_W).tag(sync=True)
    viewport_h = traitlets_mod.Int(VIEWPORT_H).tag(sync=True)
    card = traitlets_mod.Unicode("").tag(sync=True)


class VNCView:
    """One session's cockpit view: widget state + action relay.

    The view holds NO playwright objects — it asks the SessionStore for
    screenshots and dispatches actions through store methods (the exact
    code paths the tests cover with fakes).
    """

    def __init__(self, store, host: str, port: int,
                 view_width: int = DEFAULT_VIEW_W,
                 view_height: int = DEFAULT_VIEW_H,
                 autostart: bool = True):
        self._store = store
        self.host, self.port = host, port
        self.key = store.find_by_host(host, port)
        self._w = _CockpitWidget(view_width=view_width,
                                 view_height=view_height)
        self._last_png: bytes = b""
        self._last_error: str = ""
        self._alive_hint: Optional[bool] = None
        self._unobserve = None
        # comm → action relay
        self._unobserve = self._w.observe(self._on_event, names="event")
        if autostart:
            self.refresh_sync()

    # -- sync-facing helpers --------------------------------------------
    def _encode_png(self, png: bytes) -> str:
        return base64.b64encode(png).decode("ascii")

    def is_alive(self) -> bool:
        try:
            sn = self._store._sessions.get(self.key)
            return bool(sn and sn.status == "ready")
        except Exception:
            return False

    def status_line(self) -> str:
        st = "alive" if self.is_alive() else "dead"
        return f"{self.key}: {st}"

    def _mark_dead_if_down(self) -> None:
        self._w.status = "dead" if not self.is_alive() else "alive"

    # -- core: refresh (screenshot → widget) ------------------------------
    async def refresh(self) -> None:
        """Take a fresh screenshot into the widget. Fail-closed: on error
        the error card shows + last PNG is kept (dead = last screenshot +
        'session ended' card — brief §2/§4)."""
        import asyncio
        try:
            png = await asyncio.wait_for(
                self._store.screenshot(self.host, self.port),
                timeout=_REFRESH_TIMEOUT_S)
            self._last_png = png
            self._w.png_b64 = self._encode_png(png)
            self._w.error = ""
        except Exception as exc:  # noqa: BLE001 — error card, never wedge
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._w.error = self._last_error
        self._mark_dead_if_down()

    def refresh_sync(self) -> None:
        """Cell-facing wrapper: asyncio.run would fight the running loop —
        schedule the refresh instead, so it works inline in notebooks."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            self._pending = loop.create_task(self.refresh())
        else:
            asyncio.run(self.refresh())

    # -- actions (comm relay entry) ---------------------------------------
    def _on_event(self, change) -> None:
        ev = change.get("new") or {}
        if not ev:
            return
        import asyncio
        # the comm handler runs on the kernel's loop; schedule the action
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._do_action(ev))
            else:
                loop.run_until_complete(self._do_action(ev))
        except RuntimeError:
            asyncio.run(self._do_action(ev))

    async def _do_action(self, ev: Dict[str, Any]) -> None:
        """Dispatch one forwarded event through the store, bounded (the
        store already enforces its own timeouts; error card on failure)."""
        import logging
        lg = logging.getLogger("dabooks.views")
        t = ev.get("type")
        try:
            if t == "click":
                await self._store.click(self.host, self.port,
                                        int(ev.get("x", 0)), int(ev.get("y", 0)))
            elif t == "mousedown":
                await self._store.raw(self.host, self.port,
                                      lambda page: page.mouse.down())
            elif t == "mouseup":
                await self._store.raw(self.host, self.port,
                                      lambda page: page.mouse.up())
            elif t == "drag":
                await self._store.raw(self.host, self.port, lambda page: (
                    page.mouse.move(int(ev.get("x", 0)), int(ev.get("y", 0)))))
            elif t == "wheel":
                await self._store.raw(self.host, self.port, lambda page: (
                    page.mouse.wheel(int(ev.get("dx", 0)), int(ev.get("dy", 0)))))
            elif t == "key":
                # modifiers as a chord: Ctrl+a → key + held modifier presses
                mods = ev.get("modifiers") or []
                for m in mods:
                    await self._store.raw(self.host, self.port,
                                          lambda page, m=m: page.keyboard.down(m))
                await self._store.press(self.host, self.port, ev.get("key", ""))
                for m in reversed(mods):
                    await self._store.raw(self.host, self.port,
                                          lambda page, m=m: page.keyboard.up(m))
            else:
                lg.warning("unhandled event type %r", t)
                return
            # action applied; refresh the view from the new viewport state
            await self.refresh()
        except Exception as exc:  # noqa: BLE001 — error card, never wedge
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._w.error = self._last_error
            self._w.card = f"action {t!r} failed: {self._last_error}"
            lg.warning("action %s failed: %s", t, self._last_error)

    # -- plain-mode fallback (brief §5) -----------------------------------
    def render(self, mode: str = "auto"):
        """``auto`` → the anywidget; ``plain`` (or widget-failure) → a text
        fallback card. Never raises for asset problems — degrade."""
        if mode == "plain":
            return self._plain_card()
        try:
            _ = self._w.model_id  # touch the comm — assets may be absent
            return self._w
        except Exception:
            return self._plain_card()

    def _plain_card(self) -> str:
        alive = self.is_alive()
        png_note = f"last png: {len(self._last_png)} bytes" if self._last_png else "no screenshot yet"
        err = self._last_error or ("(none)" if alive else "session ended")
        return (
            f"[plain-mode fallback]\n"
            f"session : {self.key} — {'ALIVE' if alive else 'DEAD'}\n"
            f"status  : {self._w.status}\n"
            f"error   : {err}\n"
            f"{png_note}"
        )

    def dispose(self) -> None:
        if self._unobserve:
            with_context = getattr(self._w, "unobserve", None)
            import contextlib
            with contextlib.suppress(Exception):
                self._w.unobserve(self._unobserve, names="event")
            self._unobserve = None

    # repr for quick cell echo --------------------------------------------
    def __repr__(self) -> str:
        return f"VNCView({self.key}, {self.status_line()})"


# --- Phase 2B re-framed (operator decision 2026-10-13) ------------------
# The proxied-IFRAME render tier is CUT (ProxiedView/proxied_frame/
# frame_html removed — git history keeps them). Reason, measured live:
# the lane's primary deployment is a HEADLESS kernel VM — no operator eye
# rides the host loopback, so an iframe pointed at 127.0.0.1:<port> has
# no reliable renderer (VS Code remote webviews live on ANOTHER netns;
# zero proxy events during navigation — see webview-loopback-topology.md).
# The REWRITING PROXY stays: its value is the INSTRUMENTATION (URL
# rewriting + inspector injection + CSP enforcement + verdict/beacon
# logs) which playwright chromium consumes perfectly inside the session
# lane. Operator-accessible rendering = the VNC screenshot view above