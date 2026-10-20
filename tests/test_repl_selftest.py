"""Unit tests for replkit.core.run_selftest — the pytest-lane REPL macro.

Contract under test (the 2026-10-20 cwd-independence fix): the macro is
anchored to the repo root — pytest is spawned with that cwd and passthrough
args are resolved against it — so ``tool_repl.py selftest`` fires from any
operator shell directory (alias lane), not only in-REPL where the cwd
happened to already be the checkout.
"""

import subprocess
import sys
from pathlib import Path

from replkit.core import _REPO_ROOT, _anchor_pytest_arg, _selftest_argv, run_selftest


def _fake_run(monkeypatch, captures):
    """Swap subprocess.run for a recorder; return the always-exit-0 stub."""
    def fake_run(argv, **kwargs):
        captures["argv"] = argv
        captures["kwargs"] = kwargs
        result = subprocess.CompletedProcess(argv, 0)
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)


# --- spawn anchoring ---------------------------------------------------------

def test_selftest_spawns_pytest_with_repo_root_cwd(monkeypatch):
    """The macro anchors pytest's cwd to the repo root — the fix itself."""
    captures = {}
    _fake_run(monkeypatch, captures)
    monkeypatch.chdir("/")
    rc = run_selftest()
    assert rc == 0
    assert captures["kwargs"]["cwd"] == str(_REPO_ROOT)


def test_bare_selftest_targets_absolute_tests_tree():
    """No-extra runs point at <repo>/tests — not a relative ``tests/``."""
    argv = _selftest_argv()
    assert argv[-2:] == [str(_REPO_ROOT / "tests"), "-q"]
    assert argv[:3] == [sys.executable, "-m", "pytest"]


# --- passthrough arg anchoring ----------------------------------------------

def test_bare_test_name_anchors_into_tests():
    """The documented shorthand: test_scope_gate → tests/test_scope_gate.py."""
    assert _anchor_pytest_arg("test_scope_gate") == "tests/test_scope_gate.py"


def test_bare_test_name_with_node_id_keeps_node():
    assert (
        _anchor_pytest_arg("test_scope_gate::test_ffuf_blocked_raises")
        == "tests/test_scope_gate.py::test_ffuf_blocked_raises"
    )


def test_flags_and_absolute_paths_pass_untouched():
    assert _anchor_pytest_arg("-k") == "-k"
    assert _anchor_pytest_arg("--tb=short") == "--tb=short"
    assert _anchor_pytest_arg("-x") == "-x"
    assert _anchor_pytest_arg("/abs/somewhere/test_x.py") == "/abs/somewhere/test_x.py"


def test_repo_relative_path_passes_through():
    # resolves against the repo root because pytest is spawned there
    assert _anchor_pytest_arg("tests/test_scope_gate.py") == "tests/test_scope_gate.py"


def test_unknown_token_passes_through_honestly():
    # pytest surfaces its own "file or directory not found" for typos
    assert _anchor_pytest_arg("test_does_not_exist_xyz") == "test_does_not_exist_xyz"


def test_caller_cwd_file_anchors_absolute(monkeypatch, tmp_path):
    """A file that exists under the *caller's* cwd is anchored absolute."""
    local = tmp_path / "test_local.py"
    local.write_text("def test_x(): pass\n")
    monkeypatch.chdir(tmp_path)
    assert _anchor_pytest_arg("test_local.py") == str(local)


# --- macro-level behavior -----------------------------------------------------

def test_force_flags_swallowed(monkeypatch):
    captures = {}
    _fake_run(monkeypatch, captures)
    run_selftest(["--force"])
    # legacy sweep muscle memory: accepted-and-ignored → bare full run
    assert captures["argv"][-2:] == [str(_REPO_ROOT / "tests"), "-q"]


def test_partial_extra_skips_default_full_run():
    argv = _selftest_argv(["-k", "budget"])
    assert "tests" not in argv, "-k extras must not get the default tests/ tree"
    assert argv[-2:] == ["-k", "budget"]