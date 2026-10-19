"""Tests for the engagement / journal lane (2026-10-19 design).

Covers the parts that must hold with Postgres ABSENT (the additive-store
contract: nothing may break a tool) and the SQLite evidence mirror, which is
the lane that actually runs in CI/this terminal (no postgres container here).
"""

import json
import time
from pathlib import Path

import pytest

import constants

# Every engagement-lane module resolves workspace paths per-call via
# resolve_workspace_root(), so tests just point WORKSPACE_ROOT at a tmp dir
# before importing anything that touches the stores.

@pytest.fixture()
def ws_root(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    # Isolate armed-state + evidence DB under the tmp workspace.
    yield tmp_path


@pytest.fixture()
def no_postgres(monkeypatch):
    """Force the engagement DB lane to look down (no psycopg reach)."""
    import utils.engagement as eng
    monkeypatch.setattr(eng, "connect", lambda *a, **kw: None)
    yield


# ─── utils.engagement (armed state, no-DB behavior) ─────────────────────────


def test_engagement_arm_disarm_roundtrip(ws_root):
    from utils import engagement as eng

    assert eng.current_slug() is None  # disarmed by default

    state = eng.arm("blue.htb", roe_note="lab RoE")
    assert state["slug"] == "blue.htb"
    assert eng.current_slug() == "blue.htb"  # mtime cache works

    eng.disarm()
    assert eng.current_slug() is None


def test_engagement_armed_state_visible_across_callers(ws_root):
    """The armed state is file-backed so a different process 'sees' it — the
    REPL arms, the Brain picks it up on the next call (mtime check)."""
    from utils import engagement as eng

    eng.arm("foo.example")
    # Simulate the second process: clear the cache and re-read.
    eng._ARMED_CACHE["mtime"] = None
    eng._ARMED_CACHE["state"] = None
    assert eng.current_slug() == "foo.example"


def test_engagement_arm_rejects_empty_slug(ws_root):
    from utils import engagement as eng
    with pytest.raises(ValueError):
        eng.arm("   ")


def test_engagement_no_postgres_degrades_silently(ws_root, no_postgres):
    """DB down: connectivity helpers fail soft, never raise — the additive
    contract that lets every dispatch path call through without caring."""
    from utils import engagement as eng

    assert eng.connect() is None
    assert eng.sweep() == -1
    assert eng.db_status()["connected"] is False
    assert eng.current_slug() is None


# ─── utils.journal (digest, redaction, write path) ──────────────────────────


def test_journal_digest_is_tiny_and_self_contained():
    from utils.journal import result_digest

    result = {
        "status": "Success",
        "result": {
            "hosts": [{"ip": "10.0.0.5", "hostname": "blue.htb"}],
            "open_ports": [22, 80],
            "log": "x" * 5000,
        },
    }
    d = result_digest(result)
    assert isinstance(d, str)
    assert len(d) <= 200


def test_journal_redact_converts_handles_to_sessref():
    from utils.journal import _redact_args

    args = {
        "handle": "ssh:sess-0001",
        "session_id": "sess-0001",
        "session_handle": "x",
        "target": "10.0.0.5",
        "nested": {"handle": "msf:3"},
        "list": [{"session_id": "9"}],
    }
    out = _redact_args(args, "42")

    assert out["handle"] == {"$sessref": "42"}
    assert out["session_id"] == {"$sessref": "42"}
    assert out["session_handle"] == {"$sessref": "42"}  # suffix match
    assert out["nested"]["handle"] == {"$sessref": "42"}
    assert out["list"][0]["session_id"] == {"$sessref": "42"}
    assert out["target"] == "10.0.0.5"  # untouched — residency decision


def test_journal_write_is_silent_when_db_down(ws_root, no_postgres):
    from utils.journal import write_dispatch

    entry_id = write_dispatch(
        tool_id="auxiliaries.nmap.run_nmap",
        status="ok",
        lane="repl",
        args={"target": "127.0.0.1"},
        result={"status": "Success", "result": {"open_ports": [22]}},
    )
    assert entry_id is None  # skipped, not raised


def test_journal_what_touched_empty_when_db_down(ws_root, no_postgres):
    from utils.journal import what_touched
    assert what_touched("10.0.0.5") == []


# ─── utils.evidence (sqlite mirror: real behavior, real constraints) ────────


@pytest.fixture()
def evidence_store(ws_root):
    from utils.evidence import EvidenceStore
    db = str(Path(ws_root) / "evidence" / "test_evidence.db")
    store = EvidenceStore(db_path=db)
    yield store
    store.close()


def test_evidence_record_and_defaults(evidence_store):
    entry = evidence_store.record(
        "credential",
        "NTLM hash for svc_sql recovered from secretsdump",
        content={"ntlm": "31d6cfe0d16ae931b73c59d7e0c089c0"},
        engagement="blue.htb",
    )
    assert entry["entry_id"].startswith("evid:")
    assert entry["status"] == "open"
    assert "24h" in entry["ttl_note"]

    # scope questions never expire
    scope_entry = evidence_store.record(
        "scope_question", "is 10.0.0.6 in scope?", engagement="blue.htb",
    )
    assert scope_entry["expires_at"] is None

    # cred material actually lands (residency decision, not scrubbed)
    assert evidence_store.get(entry["entry_id"])["content"]["ntlm"]


def test_evidence_one_attempt_per_service_enforced(evidence_store):
    """The standing runbook rule as a constraint: second attempt refused
    with the re-mint guidance."""
    entry = evidence_store.record(
        "credential", "cred A", content={"user": "admin"},
        engagement="blue.htb",
    )
    eid = entry["entry_id"]

    first = evidence_store.add_attempt(eid, "ssh", "10.0.0.5:22", "miss")
    assert first["outcome"] == "miss"

    from utils.evidence import EvidenceError
    with pytest.raises(EvidenceError) as exc:
        evidence_store.add_attempt(eid, "ssh", "10.0.0.5:22", "hit")
    assert "re-mint" in str(exc.value)

    # different service is fine — the sweep is per-service
    second = evidence_store.add_attempt(eid, "smb", "10.0.0.5:445", "hit")
    assert second["credential_reuse_hit"] is True

    # same service, different target is also a fresh attempt slot
    third = evidence_store.add_attempt(eid, "ssh", "10.0.0.7:22", "miss")
    assert third["attempt_id"] != first["attempt_id"]


def test_evidence_resolve_promoted_requires_finding_id(evidence_store):
    from utils.evidence import EvidenceError

    entry = evidence_store.record("observation", "odd TLS cert", engagement="e")
    eid = entry["entry_id"]

    with pytest.raises(EvidenceError):
        evidence_store.resolve(eid, "promoted")  # no resolved_by -> contract break

    out = evidence_store.resolve(eid, "promoted", resolved_by="F-004")
    assert out["status"] == "promoted"
    assert out["resolved_by"] == "F-004"


def test_evidence_sweep_deletes_expired_open_only(evidence_store):
    old = evidence_store.record(
        "observation", "stale note",
        engagement="e", ttl_hours=0.000_000_3,  # ~1ms — already stale
    )
    evidence_store.record("scope_question", "never rots", engagement="e")

    time.sleep(0.05)
    # force expiry, then sweep by recording another entry (lazy path) or
    # calling _sweep directly
    evidence_store.record("observation", "fresh", engagement="e")
    gone = evidence_store._sweep()
    # 'old' entry was open+expired → gone; scope question stays
    rows = evidence_store.list_open(engagement="e")
    summaries = {r["summary"] for r in rows}
    assert "stale note" not in summaries
    assert "never rots" in summaries


def test_evidence_purge_engagement_cobweb(evidence_store):
    evidence_store.record("credential", "keep", engagement="blue.htb")
    evidence_store.record("observation", "drop", engagement="other.lab")

    n = evidence_store.purge_engagement("blue.htb")
    assert n == 1
    remaining = evidence_store.list_open(engagement="other.lab")
    assert len(remaining) == 1


def test_evidence_tools_are_framework_tools(ws_root, no_postgres):
    """The ledger tools register with the framework decorator and answer
    through the tool surface (dispatch falls back in-process on lanes that
    don't scan utils/ — state is on disk, so that's harmless)."""
    from utils.evidence import (
        record_evidence, add_validation_attempt, list_open_evidence,
        resolve_evidence,
    )
    for fn in (record_evidence, add_validation_attempt, list_open_evidence,
               resolve_evidence):
        assert getattr(fn, "_is_framework_tool", False)

    out = record_evidence(
        kind="credential",
        summary="cred B",
        content_json=json.dumps({"user": "root", "password": "hunter2"}),
    )
    assert out.startswith("Evidence evid:")
    assert "Reuse-sweep reminder" in out

    import re
    eid = re.search(r"evid:[0-9a-f]{16}", out).group(0)
    hit = add_validation_attempt(eid, "mysql", "10.0.0.5:3306", "hit")
    assert "REUSE HIT" in hit


# ─── envelope contract (daharness/models.py) ────────────────────────────────


def test_envelope_roundtrip_legacy_shape():
    from daharness.models import ExecutionEnvelope

    legacy = {
        "status": "Success",
        "result": {"open_ports": [22, 80]},
        "_elapsed_s": 1.5,
        "_provenance": {"untrusted_target": True, "source": "analysis_target"},
        "degraded": True,
    }
    env = ExecutionEnvelope.from_legacy(legacy, tool_id="auxiliaries.nmap.run_nmap")
    assert env.status == "Success"
    assert env.elapsed_s == 1.5
    assert env.provenance.untrusted_target is True
    assert env.meta.degraded is True

    mirror = env.to_legacy()
    assert mirror["status"] == "Success"
    assert mirror["result"] == {"open_ports": [22, 80]}
    assert mirror["_elapsed_s"] == 1.5
    assert mirror["_provenance"] == {"untrusted_target": True, "source": "analysis_target"}
    assert mirror["degraded"] is True


def test_envelope_budget_directive_is_top_level():
    """The budget directive lives at the model's top level so projection can
    never clobber it (today's directive is applied after projection for
    exactly this reason)."""
    from daharness.models import ExecutionEnvelope, BudgetDirective

    env = ExecutionEnvelope(
        status="budget_exhausted",
        meta={"tool_id": "x", "transport": "brain"},
        budget=BudgetDirective(kind="budget_exhausted", used=10, cap=10),
    )
    assert env.budget.used == 10
    mirror = env.to_legacy()
    # legacy mirror does NOT carry budget (old readers would ignore it) —
    # the model is the contract going forward.
    assert "budget" not in mirror


def test_envelope_meta_extra_captures_passthrough_keys():
    from daharness.models import ExecutionEnvelope

    legacy = {"status": "Success", "result": {"a": 1}, "job_id": "job-77"}
    env = ExecutionEnvelope.from_legacy(legacy, tool_id="t")
    assert env.meta.extra.get("job_id") == "job-77"