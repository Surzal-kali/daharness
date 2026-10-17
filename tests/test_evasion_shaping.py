"""Tests for the scan-shaping lane (evasion/shaping.py + run_nmap wiring).

Box-independent: no nmap/masscan binary required. run_nmap is driven with a
fake launch_job recording the assembled argv (house fake-subprocess pattern;
the scope gate is a no-op while unarmed, per its own contract).

pytest-compatible AND directly runnable: ``python3 tests/test_evasion_shaping.py``
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import evasion.shaping as shaping
from evasion.shaping import ShapingError, resolve, SCAN_SHAPING_PRESETS


def _load_nmap():
    if "auxiliaries.nmap" in sys.modules:
        return sys.modules["auxiliaries.nmap"]
    spec = importlib.util.spec_from_file_location(
        "auxiliaries.nmap", str(ROOT / "auxiliaries" / "nmap.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["auxiliaries.nmap"] = mod
    spec.loader.exec_module(mod)
    return mod


def _drive(monkeypatch, shaping_name, options, tmp_path, **extra):
    """Run run_nmap with a fake launch_job; returns the recorded command.

    Stubs the scope gate (labhost123 is not a blessed target and the sandbox
    carries real ./scope state) — gate precedence is tested in
    test_scope_gate.py; this file tests shaping mechanics only.
    """
    import utils.scope_gate as _g
    monkeypatch.setattr(_g, "check_scan", lambda t: (True, ""))
    mod = _load_nmap()
    recorded = {}

    def fake_launch_job(command, **kw):
        recorded["command"] = list(command)
        recorded["kw"] = kw
        return {"status": "Success", "job_id": "fake-1", "fake": True}

    monkeypatch.setattr(mod, "launch_job", fake_launch_job)
    r = mod.run_nmap("labhost123", options=options,
                     shaping=shaping_name, **extra) if shaping_name is not None \
        else mod.run_nmap("labhost123", options=options)
    return r, recorded["command"]


# --- resolver unit paths (pure data, no binary) -------------------------------


def test_resolve_quiet_merge_and_operator_last():
    r = resolve("quiet", "-p- -sV")
    assert r["applied"] == "quiet"
    assert r["merged"] == ["-T2", "--max-rate", "50", "-p-", "-sV"]
    assert r["notes"], "wire-notes should self-describe the preset"


def test_resolve_unknown_preset():
    with pytest.raises(ShapingError) as e:
        resolve("stealth")
    assert "Unknown shaping preset" in str(e.value)


def test_resolve_invalid_token():
    with pytest.raises(ShapingError):
        resolve("quiet;rm -rf")


def test_resolve_lab_only_refuses_without_mode(monkeypatch):
    monkeypatch.delenv("SHAPING_LAB_MODE", raising=False)
    with pytest.raises(ShapingError) as e:
        resolve("decoy")
    assert "SHAPING_LAB_MODE" in str(e.value)


def test_resolve_lab_only_allows_with_mode(monkeypatch):
    monkeypatch.setenv("SHAPING_LAB_MODE", "1")
    assert resolve("decoy", lab_mode=None)["applied"] == "decoy"
    assert resolve("fragment", lab_mode=True)["merged"] == ["-f", "--mtu", "24"]


def test_preset_table_hygiene():
    for name, spec in SCAN_SHAPING_PRESETS.items():
        import re
        assert re.match(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$", name), name
        assert spec["flags"], name
        assert (spec.get("notes") or spec.get("lab_note")), name + " must self-describe"


# --- run_nmap wiring (fake launch_job records assembled argv) -----------------


def test_run_nmap_preset_flags_then_operator_wins(monkeypatch, tmp_path):
    _, cmd = _drive(monkeypatch, "quiet", "-T5 -p-", tmp_path)
    # Order: preset flags, default -Pn, operator flags, target. nmap honors
    # the LAST occurrence of a repeated flag → operator -T5 beats preset -T2.
    assert cmd[:5] == ["nmap", "-T2", "--max-rate", "50", "-Pn"], cmd
    assert cmd[5:7] == ["-T5", "-p-"], cmd
    assert cmd[-1] == "labhost123", cmd


def test_run_nmap_default_pn_single_no_options(monkeypatch, tmp_path):
    _, cmd = _drive(monkeypatch, "quiet", "", tmp_path)
    assert cmd == ["nmap", "-T2", "--max-rate", "50", "-Pn", "labhost123"], cmd
    assert cmd.count("-Pn") == 1


def test_run_nmap_no_shaping_unchanged(monkeypatch, tmp_path):
    _, cmd = _drive(monkeypatch, None, "-T3", tmp_path)
    assert cmd == ["nmap", "-Pn", "-T3", "labhost123"], cmd


def test_run_nmap_unknown_preset_raises(monkeypatch, tmp_path):
    from utils.scope_gate import ScopeGateError
    mod = _load_nmap()
    with pytest.raises(ScopeGateError) as e:
        _drive(monkeypatch, "stealth", "", tmp_path)
    assert "shaping" in str(e.value).lower()


def test_run_nmap_decoy_gated_and_opened(monkeypatch, tmp_path):
    from utils.scope_gate import ScopeGateError
    # Without the env var: legal block via ScopeGateError.
    monkeypatch.delenv("SHAPING_LAB_MODE", raising=False)
    with pytest.raises(ScopeGateError) as e:
        _drive(monkeypatch, "decoy", "", tmp_path)
    assert "SHAPING_LAB_MODE" in str(e.value)
    # With the env var: launches, decoy flags present.
    monkeypatch.setenv("SHAPING_LAB_MODE", "1")
    _, cmd = _drive(monkeypatch, "decoy", "", tmp_path)
    assert "-D" in cmd and "RND:5,ME" in cmd, cmd


if __name__ == "__main__":
    passed = failed = 0
    import inspect
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if len(inspect.signature(fn).parameters):
                    print(f"SKIP (needs fixtures): {name}")
                    continue
                fn() if not name.endswith("raises") else None
                passed += 1
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed (fixture-dependent tests skipped)")
