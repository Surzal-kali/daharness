"""Phase-0 tests for the kernel-lane primitives (dabooks/primitives).

Scope: pure-logic only — kernelgate parse layer + sweeps demo envelope.
Playwright and the session module are Phase 1+; never imported here.
Ground-truth semantics mirror tool_repl.py's injected helpers exactly.
"""

import asyncio

import pytest

from dabooks.primitives.kernelgate import (
    _envelope_payload, _injected, _walk_find, _walk_grep, _walk_keys,
    _walk_pluck, first, find, grep, keys, pluck, to_json, wait_for,
)
from dabooks.primitives.sweeps import DEMO_SWEEP_ENVELOPE, demo_envelope, summarize_parsed


# --- fail-closed first --------------------------------------------------
def test_first_none_returns_none():
    assert first(None) is None


def test_first_empty_returns_none():
    assert first([], "empty list") is None


def test_first_non_iterable_returns_none():
    assert first(42, "scalar") is None


def test_first_hit():
    assert first([1, 2], "two") == 1


def test_first_generator_is_consumed_safely():
    assert first(iter(["a", "b"])) == "a"


# --- envelope unwrap (tool_repl ground truth) ---------------------------
def test_envelope_payload_unwraps_status_result():
    env = {"status": "ok", "result": {"x": 1}}
    assert _envelope_payload(env) == {"x": 1}


def test_envelope_payload_passthrough_non_envelope():
    assert _envelope_payload({"x": 1}) == {"x": 1}


def test_envelope_payload_bare_result_unwrapped():
    # tool_repl truth: "result" present (even a bare string) → returned as-is
    env = {"status": "ok", "result": "rawtext"}
    assert _envelope_payload(env) == "rawtext"


def test_envelope_payload_stdout_json_fallback():
    env = {"status": "ok", "stdout": '{"routes": ["/a"]}'}
    assert _envelope_payload(env) == {"routes": ["/a"]}


def test_envelope_payload_stdout_raw_string():
    env = {"status": "ok", "stdout": "not json at all"}
    assert _envelope_payload(env) == "not json at all"


# --- pluck: list-values-whole, dicts-dropped (ground truth) ------------
def test_pluck_list_values_kept_whole_not_flattened():
    # THE regression: injected pluck returns the list as ONE entry.
    env = demo_envelope()
    assert pluck(env, "routes") == [["/", "/admin/", "/static/app.js"]]


def test_pluck_scalars_at_depth():
    assert sorted(pluck(demo_envelope(), "port")) == [22, 80]


def test_pluck_drops_dicts():
    obj = {"banners": [{"raw": "x"}, {"raw": "y"}], "deep": {"banners": [{"raw": "z"}]}}
    assert pluck(obj, "banners") == [[{"raw": "x"}, {"raw": "y"}], [{"raw": "z"}]]


def test_pluck_missing_key_is_empty_not_error():
    assert pluck(demo_envelope(), "nonexistent") == []


# --- keys: sorted distinct ----------------------------------------------
def test_keys_are_sorted():
    ks = keys(demo_envelope())
    assert ks == sorted(ks)


def test_keys_includes_all_levels():
    names = keys(demo_envelope())
    for expected in ("port", "routes", "sweep_id", "banner", "meta"):
        assert expected in names


# --- find: bare values + {key,value,path} with loose equality -----------
def test_find_bare_values():
    hits = find(demo_envelope(), "banner")
    assert len(hits) == 2
    assert all(isinstance(h, dict) for h in hits)  # each banner value IS a dict result


def test_find_with_value_returns_path_dicts():
    hits = find(demo_envelope(), "port", value=80)
    assert len(hits) == 1
    assert hits[0]["value"] == 80
    assert "path" in hits[0]


def test_find_loose_str_coercion():
    # find(r, "port", value="80") must match int 80 — the _value_matches rule
    hits = find(demo_envelope(), "port", value="80")
    assert len(hits) == 1
    assert hits[0]["value"] == 80


# --- grep: multi-line split, key matching, sorted -----------------------
def test_grep_finds_banner_string():
    hits = grep(demo_envelope(), r"OpenSSH")
    assert len(hits) == 1
    path, value = hits[0]
    assert "banner" in path
    assert "OpenSSH_8.4p1" in value


def test_grep_matches_dict_keys_too():
    hits = grep(demo_envelope(), r"^sweep_id$")
    assert any("<key>" in line for _, line in hits)


def test_grep_is_sorted_by_path():
    hits = grep(demo_envelope(), r".")
    paths = [p for p, _ in hits]
    assert paths == sorted(paths)


def test_grep_case_insensitive_default():
    assert grep(demo_envelope(), r"nginx")
    assert grep(demo_envelope(), r"NGINX")


# --- sweeps fixture -----------------------------------------------------
def test_demo_envelope_is_a_fresh_copy():
    a = demo_envelope()
    b = demo_envelope()
    a["result"]["target"] = " Mutated"
    assert b["result"]["target"] == "192.0.2.10"


def test_demo_envelope_declares_fixtures_not_tools():
    assert DEMO_SWEEP_ENVELOPE["result"]["meta"]["scanner"] == "synthetic"


def test_summarize_parsed_stable_lines():
    lines = summarize_parsed(demo_envelope())
    assert lines == ["port 80 → http", "port 22 → ssh"]


# --- wait_for (the sole async member) -----------------------------------
def test_wait_for_returns_value():
    async def ok():
        return 7
    assert asyncio.run(wait_for(ok(), 1.0, "test")) == 7


def test_wait_for_timeout_fails_closed():
    async def sleepy():
        await asyncio.sleep(5)
    assert asyncio.run(wait_for(sleepy(), 0.01, "timeout-case")) is None


# --- purity guard (the graduation seam) ---------------------------------
def test_kernel_lane_package_never_imports_framework():
    """Kernel-lane purity, probed in a clean subprocess.

    Scanning the running pytest process's sys.modules is order-dependent:
    any earlier test in the suite that imports framework modules poisons
    the check (a false positive). The honest probe imports the kernel-lane
    package in a fresh interpreter and scans THAT process's sys.modules.
    """
    import ast
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    probe = (
        "import sys\n"
        "from dabooks.primitives import kernelgate, sweeps\n"
        "offender = [m for m in sys.modules\n"
        "            if m.startswith(('constants', 'daharness',\n"
        "                             'utils.session_manager', 'utils.findings'))]\n"
        "print(repr(offender))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, cwd=str(repo),
        env={**os.environ, "PYTHONPATH": str(repo)},
        timeout=60,
    )
    assert proc.returncode == 0, f"kernel-lane import probe failed: {proc.stderr}"
    offender = ast.literal_eval(proc.stdout.strip())
    assert offender == [], f"kernel-lane purity violated by: {offender}"


def test_kernel_lane_package_has_no_framework_tool_decorators():
    import ast
    from pathlib import Path
    pkg = Path(__file__).resolve().parent.parent / "dabooks" / "primitives"
    offenders = []
    for py in pkg.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                names.append(node.func.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                names.append(node.func.attr)
            if any(n == "framework_tool" for n in names):
                offenders.append(py.name)
    assert offenders == [], f"@framework_tool leaked into kernel lane: {offenders}"


# --- the await-trap regression (this bit us once, live) ----------------
def test_injected_coroutine_rejected_loudly():
    """If an injected helper returns a coroutine, pluck must raise a clear
    TypeError — not hand back a half-consumed coroutine."""
    import dabooks.primitives.kernelgate as kg

    async def fake_pluck(env, key):
        return []

    real = kg._injected
    try:
        kg._injected = lambda name: fake_pluck if name == "pluck" else None
        with pytest.raises(TypeError, match="is coroutine|coroutine"):
            pluck({"status": "ok", "result": {"routes": ["/x"]}}, "routes")
    finally:
        kg._injected = real


# --- the self-shadow regression (hit live 2026-10-13) ------------------
def test_injected_refuses_our_own_shim_functions():
    """A notebook cell doing `from kernelgate import pluck` overwrites the
    injected pluck in user_ns. _injected must refuse our own function —
    otherwise it calls itself forever (RecursionError)."""
    import dabooks.primitives.kernelgate as kg

    class FakeShell:
        def __init__(self, ns):
            self.user_ns = ns

    class FakeIP:
        @staticmethod
        def get_ipython():
            return FakeShell({"pluck": kg.pluck, "keys": kg.keys})

    import sys
    saved = sys.modules.get("IPython")
    real = kg._injected

    class FakeIPModule:
        get_ipython = staticmethod(lambda: FakeShell({"pluck": kg.pluck, "keys": kg.keys}))

    try:
        sys.modules["IPython"] = FakeIPModule
        assert kg._injected("pluck") is None  # own shim ignored
        assert kg._injected("keys") is None
        # list values stay WHOLE per ground truth (one entry per occurrence)
        pluck_env = {"status": "ok", "result": {"routes": ["/x"]}}
        assert pluck(pluck_env, "routes") == [["/x"]]
    finally:
        if saved is not None:
            sys.modules["IPython"] = saved
        else:
            sys.modules.pop("IPython", None)
        kg._injected = real


def test_injected_accepts_non_shadowed_helpers():
    """A foreign helper of a NON-OWN name is still returned (the guard is
    name-scoped and identity-scoped, not a blanket ban)."""
    import dabooks.primitives.kernelgate as kg

    class FakeShell:
        def __init__(self, ns):
            self.user_ns = ns

    def foreign_helper():
        return []

    class FakeIPModule:
        get_ipython = staticmethod(lambda: FakeShell({"pluck": foreign_helper}))

    import sys
    saved = sys.modules.get("IPython")
    real = kg._injected
    try:
        sys.modules["IPython"] = FakeIPModule
        assert kg._injected("pluck") is foreign_helper
    finally:
        if saved is not None:
            sys.modules["IPython"] = saved
        else:
            sys.modules.pop("IPython", None)
        kg._injected = real