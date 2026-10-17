"""Tests for the unknown ledger (open-questions store + framework tools).

Covers: table creation via FindingStore._init_table, add/list/resolve
semantics, domain and status validation, promoted-requires-finding rule,
lost-id race handling, and the render_markdown open-questions appendix
(including the no-findings-yet path).
"""

import pytest

from daharness.findings import FindingStore
from daharness.models import Unknown


@pytest.fixture
def store(tmp_path):
    s = FindingStore(str(tmp_path / "ids.db"))
    yield s
    s.close()


class TestUnknownTableAndAdd:
    def test_unknowns_table_created(self, store):
        tables = {
            r[0]
            for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "unknowns" in tables

    def test_add_returns_sequential_ids(self, store):
        u1 = store.add_unknown("Port 3306 open — unprobed", domain="recon", asset="10.0.0.5")
        u2 = store.add_unknown("creds not yet validated", domain="creds")
        assert u1.id == "U-001"
        assert u2.id == "U-002"
        assert u1.status == "open"
        assert u1.domain == "recon"
        assert u1.ts  # ISO-8601 timestamp set

    def test_add_rejects_bad_domain(self, store):
        with pytest.raises(ValueError):
            store.add_unknown("x", domain="bogus")

    def test_add_domain_normalized_lowercase(self, store):
        u = store.add_unknown("x", domain="RECON")
        assert u.domain == "recon"

    def test_findings_table_unaffected(self, store):
        """The ledger table coexists with findings in the same DB."""
        f = store.add(title="SQLi", severity="P1", asset="http://x")
        assert f.id == "F-001"
        assert store.count() == 1
        assert store.unknown_count() == 0


class TestUnknownListAndGet:
    def test_list_filters(self, store):
        store.add_unknown("a web question", domain="web", asset="10.0.0.7")
        store.add_unknown("a creds question", domain="creds", asset="10.0.0.5")
        store.add_unknown("a recon question", domain="recon")
        assert len(store.unknowns()) == 3
        assert len(store.unknowns(domain="creds")) == 1
        assert len(store.unknowns(asset="10.0.0.7")) >= 1
        # substring match on asset
        assert len(store.unknowns(asset="0.0.7")) == 1

    def test_list_include_closed(self, store):
        u = store.add_unknown("q", domain="general")
        store.resolve_unknown(u.id, "resolved", resolution="answered")
        assert store.unknowns(include_closed=False) == []
        assert len(store.unknowns(include_closed=True)) == 1
        assert store.unknowns(status="resolved")[0].status == "resolved"

    def test_get_unknown(self, store):
        u = store.add_unknown("q", domain="web")
        got = store.get_unknown(u.id)
        assert got is not None and got.id == u.id
        assert store.get_unknown("U-999") is None

    def test_id_ordering_numeric(self, store):
        """U-010 must sort after U-009 (numeric, not lexicographic)."""
        for i in range(12):
            store.add_unknown(f"q{i}")
        ids = [u.id for u in store.unknowns()]
        assert ids.index("U-010") > ids.index("U-009")


class TestResolveUnknown:
    def test_resolve_wontfix(self, store):
        u = store.add_unknown("q")
        got = store.resolve_unknown(u.id, "wontfix", resolution="out of scope")
        assert got.status == "wontfix"
        assert got.resolution == "out of scope"
        assert got.closed_ts

    def test_resolve_rejects_bad_status(self, store):
        u = store.add_unknown("q")
        with pytest.raises(ValueError):
            store.resolve_unknown(u.id, "bogus")

    def test_promoted_requires_finding_id(self, store):
        u = store.add_unknown("q")
        with pytest.raises(ValueError):
            store.resolve_unknown(u.id, "promoted")
        got = store.resolve_unknown(u.id, "promoted", resolved_by="F-004")
        assert got.status == "promoted"
        assert got.resolved_by == "F-004"

    def test_resolve_missing_returns_none(self, store):
        assert store.resolve_unknown("U-999", "resolved") is None


class TestLedgerTools:
    """The @framework_tool wrappers (utils/findings.py) end-to-end."""

    def test_record_and_resolve_tools(self, tmp_path, monkeypatch):
        import utils.findings as uf
        monkeypatch.setattr(
            uf, "FindingStore",
            lambda: FindingStore(str(tmp_path / "ids.db")),
        )
        out = uf.record_unknown(
            question="Port 3306 open on 10.0.0.5 — unprobed",
            domain="recon", asset="10.0.0.5", tool_chain="run_nmap",
        )
        assert "U-001" in out and "recorded" in out
        listing = uf.list_unknowns()
        assert "U-001" in listing and "unprobed" in listing
        resolved = uf.resolve_unknown("U-001", "resolved", resolution="mysql probes queued")
        assert "marked 'resolved'" in resolved
        # bad domain lands as a clean error string, not an exception
        err = uf.record_unknown(question="x", domain="nope")
        assert err.startswith("Error:")

    def test_resolved_default_listing_hides_closed(self, tmp_path, monkeypatch):
        import utils.findings as uf
        monkeypatch.setattr(
            uf, "FindingStore",
            lambda: FindingStore(str(tmp_path / "ids.db")),
        )
        uf.record_unknown(question="q", domain="web")
        uf.resolve_unknown("U-001", "wontfix")
        assert "no unknowns" in uf.list_unknowns()  # default status=open
        assert "U-001" in uf.list_unknowns(include_closed=True)


class TestRenderAppendix:
    def test_appendix_when_open_unknowns(self, store):
        store.add_unknown("second pass on 10.0.0.7?", domain="web", asset="10.0.0.7")
        md = store.render_markdown()
        assert "Open questions (unresolved)" in md
        assert "10.0.0.7" in md
        assert "U-001" in md

    def test_no_appendix_when_all_resolved(self, store):
        u = store.add_unknown("q")
        store.resolve_unknown(u.id, "resolved", resolution="done")
        assert "Open questions" not in store.render_markdown()

    def test_appendix_with_no_findings(self, store):
        """Early-days engagement: ledger threads render without a findings table."""
        store.add_unknown("q", domain="web")
        md = store.render_markdown()
        assert "no findings reported yet" in md
        assert "Open questions (unresolved)" in md
        assert "| ID |" not in md

    def test_appendix_coexists_with_findings_table(self, store):
        store.add(title="T", severity="P2", asset="x")
        store.add_unknown("q", domain="web")
        md = store.render_markdown()
        assert "| ID |" in md
        assert "Open questions (unresolved)" in md

    def test_bare_report_unchanged(self, store):
        assert store.render_markdown() == "_(no findings reported yet)_"


class TestUnknownModel:
    def test_defaults(self):
        u = Unknown(id="U-001", question="q", ts="2026-10-16T00:00:00+00:00")
        assert u.status == "open"
        assert u.domain == "general"
        assert u.resolution == ""
        assert u.closed_ts == ""