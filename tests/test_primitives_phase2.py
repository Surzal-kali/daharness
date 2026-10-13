"""Phase-2 tests: VNCView — trait flow, coordinate scaling, fail-closed
cards, plain-mode fallback. Fakes only; no chromium in tests.
Async tests use asyncio.run wrappers (no pytest-asyncio in this venv).
"""

import asyncio
import base64

import pytest

from dabooks.primitives.sessions import (
    LogPolicy, Session, SessionStore, default_policy,
)


def _run(coro):
    return asyncio.run(coro)


# --- fakes ---------------------------------------------------------------
class FakePage:
    def __init__(self):
        self.clicks = []
        self.keys = []
        self.down = 0
        self.up = 0
        self.moves = []
        self.wheel_calls = []

    async def goto(self, url, **k):
        self.url = url

    async def screenshot(self, full_page=False):
        return b"png-%d" % len(self.clicks)

    async def mouse_click(self, x, y):
        self.clicks.append((x, y))


class MouseAPI:
    def __init__(self, page):
        self._page = page

    async def click(self, x, y):
        await self._page.mouse_click(x, y)

    async def down(self):
        self._page.down += 1

    async def up(self):
        self._page.up += 1

    async def move(self, x, y):
        self._page.moves.append((x, y))

    async def wheel(self, dx, dy):
        self._page.wheel_calls.append((dx, dy))


class KeyboardAPI:
    def __init__(self, page):
        self._page = page

    async def press(self, key):
        self._page.keys.append(key)

    async def down(self, key):
        self._page.keys.append("down:" + key)

    async def up(self, key):
        self._page.keys.append("up:" + key)


def _ready_store():
    st = SessionStore(action_timeout_s=1.0)
    page = FakePage()
    page.mouse, page.keyboard = MouseAPI(page), KeyboardAPI(page)
    page.url = "http://10.0.0.1:80"
    sn = Session(key="10.0.0.1:80", created_at=0, last_used=0,
                 policy=default_policy(), status="ready")
    sn.page = page
    sn.route_handler = lambda r: None
    st._sessions["10.0.0.1:80"] = sn
    return st, sn


def _mk_view(store, **kw):
    import dabooks.primitives.views as views
    return views.VNCView(store, "10.0.0.1", 80, autostart=False, **kw)


# --- widget basics -------------------------------------------------------
def test_view_constructs_and_traits_exist():
    st, _ = _ready_store()
    v = _mk_view(st)
    w = v._w
    assert w.png_b64 == "" and w.status == "boot"
    assert w.viewport_w == 1280 and w.viewport_h == 800
    assert w.view_width == 960  # default render width


def test_status_line_alive():
    st, _ = _ready_store()
    v = _mk_view(st)
    assert v.status_line() == "10.0.0.1:80: alive"
    assert v.is_alive() is True


# --- refresh: screenshot → trait ----------------------------------------
def test_refresh_encodes_png():
    async def run():
        st, _ = _ready_store()
        v = _mk_view(st)
        await v.refresh()
        assert v._w.png_b64 == base64.b64encode(b"png-0").decode()
        assert v._w.error == ""
        assert v._w.status == "alive"
    _run(run())


def test_refresh_fail_closed_keeps_last_png():
    async def run():
        st, sn = _ready_store()
        v = _mk_view(st)
        await v.refresh()                      # good frame
        first = v._w.png_b64
        sn.status = "dead"                     # session dies
        await v.refresh()                      # must fail-closed, keep frame
        assert v._w.error != ""                # error card present
        assert v._w.png_b64 == first           # LAST screenshot kept
        assert v._w.status == "dead"
    _run(run())


# --- action relay --------------------------------------------------------
def test_click_action_dispatches_scaled():
    async def run():
        st, sn = _ready_store()
        v = _mk_view(st)
        await v._do_action({"type": "click", "x": 100, "y": 50})
        assert sn.page.clicks == [(100, 50)]
        assert v._w.error == ""
        # click triggers a post-action refresh (screenshot updated)
        assert v._w.png_b64 != ""
    _run(run())


def test_key_and_chord_actions():
    async def run():
        st, sn = _ready_store()
        v = _mk_view(st)
        await v._do_action({"type": "key", "key": "a",
                            "modifiers": ["Control"]})
        assert sn.page.keys == ["down:Control", "a", "up:Control"]
    _run(run())


def test_wheel_and_drag_actions():
    async def run():
        st, sn = _ready_store()
        v = _mk_view(st)
        await v._do_action({"type": "wheel", "dx": 0, "dy": 300})
        await v._do_action({"type": "drag", "x": 10, "y": 20})
        assert sn.page.wheel_calls == [(0, 300)]
        assert sn.page.moves == [(10, 20)]
    _run(run())


def test_mouse_down_up_actions():
    async def run():
        st, sn = _ready_store()
        v = _mk_view(st)
        await v._do_action({"type": "mousedown"})
        await v._do_action({"type": "mouseup"})
        assert (sn.page.down, sn.page.up) == (1, 1)
    _run(run())


def test_failed_action_sets_error_card_not_raises():
    async def run():
        st, _ = _ready_store()
        st.click = None  # break the path — the relay must catch, not raise
        v = _mk_view(st)
        await v._do_action({"type": "click", "x": 1, "y": 1})
        assert v._w.error != ""
        assert "failed" in v._w.card
    _run(run())


def test_unhandled_event_type_is_ignored():
    async def run():
        st, _ = _ready_store()
        v = _mk_view(st)
        await v._do_action({"type": "quantum-tunnel"})  # no raise
        assert v._w.error == ""
    _run(run())


# --- status/dead cards ---------------------------------------------------
def test_dead_view_renders_session_ended_card():
    st, sn = _ready_store()
    sn.status = "dead"
    v = _mk_view(st)
    v._mark_dead_if_down()
    assert v._w.status == "dead"
    card = v.render("plain")
    assert "DEAD" in card and "plain-mode" in card


def test_plain_mode_fallback_is_text():
    st, _ = _ready_store()
    v = _mk_view(st)
    out = v.render("plain")
    assert "10.0.0.1:80" in out
    assert "ALIVE" in out


# --- coordinate math (the scale contract) -------------------------------
def test_scale_constants_are_1_to_1_with_viewport():
    # Phase 1 pins deviceScaleFactor=1 at 1280x800; the view's defaults
    # must carry that exactly so widget→viewport math is a plain ratio.
    import dabooks.primitives.views as views
    assert (views.VIEWPORT_W, views.VIEWPORT_H) == (1280, 800)


def test_store_context_pins_device_scale_factor():
    # the CONTRACT lives in sessions._spawn_task — assert the kwarg is there
    import inspect
    from dabooks.primitives import sessions
    src = inspect.getsource(sessions.SessionStore._spawn_task)
    assert "device_scale_factor=1" in src


# --- purity -------------------------------------------------------------
def test_views_module_no_framework_imports():
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "dabooks" / "primitives"
           / "views.py").read_text()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                if n.split(".")[0] in ("daharness", "constants", "utils", "auxiliaries"):
                    offenders.append(n)
    assert offenders == [], offenders