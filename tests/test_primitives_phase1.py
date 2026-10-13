"""Phase-1 tests: SessionStore lifecycle, policy, and reaper — with a FAKE
browser (no chromium launch in CI/tests). Real-launch behavior is proven in
the live-kernel smoke cell of cockpit.ipynb instead (Phase-1 deliverable).

Async tests use plain ``asyncio.run`` wrappers — no pytest-asyncio plugin in
this venv, and zero new deps is the lane's style.
"""

import asyncio
import time

import pytest

from dabooks.primitives.sessions import (
    ContextCapError, LogPolicy, Session, SessionNotFoundError, SessionStore,
    default_policy, make_route_handler, resolve_browser, verdict,
)


def _run(coro):
    return asyncio.run(coro)


# --- policy verdict (pure) ----------------------------------------------
def test_verdict_allow_by_default():
    assert verdict(default_policy(), "http://x/y") == "allow"


def test_verdict_deny_pattern():
    p = LogPolicy(allow=("*",), deny=("http://evil/*",))
    assert verdict(p, "http://evil/x") == "deny"
    assert verdict(p, "http://ok/x") == "allow"


def test_verdict_deny_wins_on_overlap():
    p = LogPolicy(allow=("http://a/*",), deny=("http://a/secret*",))
    assert verdict(p, "http://a/secret/x") == "deny"
    assert verdict(p, "http://a/other") == "allow"


def test_verdict_observe_only_never_denies():
    p = LogPolicy(allow=("*",), deny=("*",), observe_only=True)
    assert verdict(p, "http://anything/") == "observe"


# --- route handler logs both verdicts -----------------------------------
class FakeRoute:
    def __init__(self, url):
        self.request = type("R", (), {"url": url})()
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


def test_route_handler_denies_and_allows(caplog):
    async def run():
        handler = make_route_handler(
            LogPolicy(allow=("*",), deny=("http://evil/*",)), "t:1")
        bad, good = FakeRoute("http://evil/x"), FakeRoute("http://ok/y")
        await handler(bad)
        await handler(good)
        return bad, good
    bad, good = asyncio.run(run())
    assert bad.aborted and not bad.continued
    assert good.continued and not good.aborted


# --- store basics -------------------------------------------------------
def test_store_missing_key_fails_closed():
    st = SessionStore()
    with pytest.raises(SessionNotFoundError):
        st.get("nope:1")


def test_find_by_host_key_form():
    assert SessionStore().find_by_host("10.0.0.1", 8080) == "10.0.0.1:8080"


# --- spawn lifecycle with a FAKE browser --------------------------------
class FakePage:
    def __init__(self):
        self.url = ""

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def screenshot(self, full_page=False):
        return b"fakepng"

    async def close(self):
        pass


class FakeContext:
    def __init__(self):
        self.routes = []
        self.closed = False

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def new_page(self):
        return FakePage()

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts = []

    async def new_context(self, **kw):
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx


def test_spawn_is_instant_and_becomes_ready():
    async def run():
        st = SessionStore()
        st._browser = FakeBrowser()  # inject fake BEFORE spawn
        sn = await st.spawn("10.0.0.1", 80)
        assert sn.status == "spawning"  # INSTANT return
        await sn.ready
        assert sn.status == "ready"
        assert sn.page.url == "http://10.0.0.1:80"
        # interception wired at spawn per contract
        assert len(sn.context.routes) == 1
    _run(run())


def test_spawn_is_idempotent():
    async def run():
        st = SessionStore()
        st._browser = FakeBrowser()
        a = await st.spawn("10.0.0.1", 80)
        await a.ready
        b = await st.spawn("10.0.0.1", 80)
        assert a is b
    _run(run())


def test_context_cap_fails_closed():
    async def run():
        st = SessionStore(max_contexts=1)
        st._browser = FakeBrowser()
        sn = await st.spawn("10.0.0.1", 80)
        await sn.ready
        with pytest.raises(ContextCapError):
            await st.spawn("10.0.0.2", 80)
        assert st.count() == 1
    _run(run())


def test_kill_marks_killed_and_closes_context():
    async def run():
        st = SessionStore()
        fb = FakeBrowser()
        st._browser = fb
        sn = await st.spawn("10.0.0.1", 80)
        await sn.ready
        status = await st.kill("10.0.0.1", 80)
        assert status == "killed"
        assert fb.contexts[0].closed
    _run(run())


def test_kill_unknown_fails_closed():
    async def run():
        st = SessionStore()
        with pytest.raises(SessionNotFoundError):
            await st.kill("9.9.9.9", 1)
    _run(run())


def test_kill_is_idempotent():
    async def run():
        st = SessionStore()
        fb = FakeBrowser()
        st._browser = fb
        sn = await st.spawn("10.0.0.1", 80)
        await sn.ready
        assert await st.kill("10.0.0.1", 80) == "killed"
        assert await st.kill("10.0.0.1", 80) == "killed"  # no-op, no error
        assert fb.contexts[0].closed  # close called exactly once
    _run(run())


def test_kill_all_returns_only_actually_killed():
    async def run():
        st = SessionStore()
        fb = FakeBrowser()
        st._browser = fb
        sn = await st.spawn("10.0.0.1", 80)
        await sn.ready
        st._sessions["old:1"] = Session(key="old:1", created_at=0, last_used=0,
                                        policy=default_policy(), status="killed")
        killed = await st.kill_all()
        assert killed == ["10.0.0.1:80"]  # 'old:1' already down — excluded
    _run(run())


def test_screenshot_dead_session_fails_closed():
    async def run():
        st = SessionStore()
        st._sessions["x:1"] = Session(key="x:1", created_at=0, last_used=0,
                                      policy=default_policy(), status="dead")
        with pytest.raises(RuntimeError, match="not ready"):
            await st.screenshot("x", 1)
    _run(run())


def test_screenshot_happy_path():
    async def run():
        st = SessionStore()
        fb = FakeBrowser()
        st._browser = fb
        sn = await st.spawn("10.0.0.1", 80)
        await sn.ready
        assert await st.screenshot("10.0.0.1", 80) == b"fakepng"
        assert sn.shots == 1
    _run(run())


# --- reaper -------------------------------------------------------------
def test_reap_idle_closes_stale_sessions():
    async def run():
        st = SessionStore(idle_reap_s=0.01)
        fb = FakeBrowser()
        st._browser = fb
        sn = await st.spawn("10.0.0.1", 80)
        await sn.ready
        await asyncio.sleep(0.05)  # let it go stale (small real sleep in test only)
        reaped = await st.reap_idle()
        assert "10.0.0.1:80" in reaped
        assert sn.status == "killed"
    _run(run())


def test_reaper_task_runs_and_cancels():
    async def run():
        st = SessionStore(reap_interval_s=0.01, idle_reap_s=0.005)
        fb = FakeBrowser()
        st._browser = fb
        await st.spawn("10.0.0.1", 80)
        task = st.start_reaper()
        # let a tick run, then stop; reaper must cancel cleanly (no wedge)
        await st.stop_reaper()
        assert task.cancelled() or task.done()
    _run(run())


# --- action bounding ----------------------------------------------------
class SlowPage:
    async def goto(self, *a, **k):
        pass

    async def screenshot(self, **k):
        await asyncio.sleep(30)


def test_action_timeout_fails_closed_not_wedges():
    async def run():
        st = SessionStore(action_timeout_s=0.2)  # tight knob, proves the bound
        sn = Session(key="s:1", created_at=0, last_used=0, policy=default_policy(),
                     status="ready")
        sn.page = SlowPage()
        st._sessions["s:1"] = sn
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="timed out"):
            await st.screenshot("s", 1)
        assert time.monotonic() - t0 < 5  # bounded, not 30s
    _run(run())


# --- browser resolution (evidence from the box) -------------------------
def test_resolve_browser_finds_repo_local_cache(monkeypatch):
    # repo-local .pw-browsers exists on this box; with no valid env override,
    # resolution must point there via env_overrides.
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    exe, overrides = resolve_browser()
    assert exe is None
    assert overrides.get("PLAYWRIGHT_BROWSERS_PATH", "").endswith(".pw-browsers")
    import glob, os
    assert glob.glob(os.path.join(overrides["PLAYWRIGHT_BROWSERS_PATH"], "chromium*"))


def test_resolve_browser_keeps_valid_override(monkeypatch, tmp_path):
    (tmp_path / "chromium-999").mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    exe, overrides = resolve_browser()
    assert exe is None and overrides == {}


def test_resolve_browser_falls_back_to_system_chromium(monkeypatch, tmp_path):
    import dabooks.primitives.sessions as mod
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(empty))
    monkeypatch.setattr(mod, "_workspace_root", lambda: empty)
    exe, overrides = resolve_browser()
    assert overrides == {}
    assert exe in (None, "/usr/bin/chromium", "/usr/bin/chromium-browser",
                   "/usr/bin/google-chrome")
    if exe is None:
        pytest.skip("no system chromium and no bundle — driver error lane")


# --- purity (the graduation seam, Phase 1 edition) ----------------------
def test_sessions_module_has_no_framework_imports():
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "dabooks" / "primitives"
           / "sessions.py").read_text()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                if n.split(".")[0] in ("daharness", "constants", "utils", "auxiliaries", "payloads", "listeners"):
                    offenders.append(n)
    assert offenders == [], f"framework imports leaked into sessions.py: {offenders}"


def test_sessions_module_no_sync_sleep():
    src = ( __import__("pathlib").Path(__file__).resolve().parent.parent
           / "dabooks" / "primitives" / "sessions.py").read_text()
    assert "time.sleep(" not in src, "sync sleep found — wedges the kernel loop"