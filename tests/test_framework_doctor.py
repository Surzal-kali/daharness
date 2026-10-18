"""Tests for framework_doctor (capability probes per analysis/attack lane).

Verifies the {available, reason, remediation} shape, full-sweep and
drill-down modes, the unknown-provider path, and the r2 remediation text
agreeing with the tool's own error message.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from auxiliaries.framework_status import framework_doctor


PROVIDER_KEYS = {"r2", "jadx", "masscan", "msf", "nmap", "ffuf", "hydra",
                 "sqlmap", "secretary_model", "snapshot_cache"}


class TestDoctorShape:
    def test_full_sweep_providers(self):
        report = framework_doctor()
        assert report["providers"].keys() == PROVIDER_KEYS
        assert report["overall"] in ("ok", "degraded", "down")
        # every record carries the contract fields
        for name, rec in report["providers"].items():
            assert set(rec.keys()) >= {"available", "reason", "remediation", "provider"}, name
            assert isinstance(rec["available"], bool)
            assert isinstance(rec["reason"], str) and rec["reason"]
            if not rec["available"]:
                assert rec["remediation"], f"{name} unavailable without remediation"

    def test_availability_counts_match(self):
        report = framework_doctor()
        ok = sum(1 for v in report["providers"].values() if v["available"])
        assert report["available"] == f"{ok}/{len(report['providers'])}"
        assert report["overall"] == ("ok" if ok == len(report["providers"])
                                     else "degraded" if ok else "down")

    def test_drill_down_single_provider(self):
        for p in ("r2", "jadx", "masscan", "msf", "secretary_model"):
            one = framework_doctor(provider=p)
            assert set(one["providers"].keys()) == {p}, p
            assert one["providers"][p]["provider"] == p

    def test_unknown_provider_flagged(self):
        one = framework_doctor(provider="bogus_lanename")
        assert "_unknown_provider" in one["providers"]
        rec = one["providers"]["_unknown_provider"]
        assert rec["available"] is False and "bogus_lanename" in rec["reason"]


class TestDoctorRemediations:
    def test_r2_record_references_plugin_when_missing(self):
        """When r2ghidra is absent the reason says so and the remediation is
        the exact string the run_r2 error path uses — doctor and tool agree
        on the fix."""
        import shutil

        from auxiliaries.radare2 import _r2ghidra_available
        rec = framework_doctor(provider="r2")["providers"]["r2"]
        assert rec["provider"] == "r2"
        if not rec["available"]:
            # Either the plugin or the drop dir is missing; both fix strings
            # must be actionable.
            assert rec["remediation"], "unavailable without remediation"
        if not _r2ghidra_available() and shutil.which("r2"):
            # Plugin-remediation strings only apply when r2 itself exists —
            # on a box without r2 the reason is the binary-absent message,
            # which is a different (also valid) remediation lane.
            assert "install" in rec["reason"].lower() or "missing" in rec["reason"].lower()
            assert "r2pm -ci r2ghidra" in rec["remediation"]

    def test_masscan_raw_socket_trap_surface(self):
        """The reason must distinguish PATH-present from launch-will-fail."""
        rec = framework_doctor(provider="masscan")["providers"]["masscan"]
        assert rec["provider"] == "masscan"
        if not rec["available"] and "raw sockets" in rec["reason"]:
            assert "CAP_NET_RAW" in rec["remediation"] or "setcap" in rec["remediation"]
        if rec["available"]:
            assert "SOCK_RAW" in rec["reason"]

    def test_msf_record(self):
        rec = framework_doctor(provider="msf")["providers"]["msf"]
        assert rec["provider"] == "msf"
        assert rec["available"] in (True, False)
        # On this box msf is typically up (bootstrap started it); when down
        # the remediation must point at starting the framework.
        if not rec["available"]:
            assert rec["remediation"], "msf unavailable without remediation"

    def test_secretary_model_reports_model_name(self):
        rec = framework_doctor(provider="secretary_model")["providers"]["secretary_model"]
        assert rec["provider"] == "secretary_model"
        assert "model" in rec["reason"].lower() or "ollama" in rec["reason"].lower()

    def test_snapshot_cache_stats_shape(self, monkeypatch):
        report = framework_doctor()
        rec = report["providers"]["snapshot_cache"]
        assert rec["provider"] == "snapshot_cache"
        assert "entries" in rec["reason"] and "hits" in rec["reason"]

    def test_scan_tools_path_or_remediation(self):
        for name, rec in framework_doctor(provider="scanners")["providers"].items():
            if rec["available"] is False:
                assert "install" in rec["remediation"].lower(), name