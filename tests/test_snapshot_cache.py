"""Tests for the analysis snapshot cache (utils/snapshot_cache.py).

Covers the store contract (put/lookup/cap/TTL/stats/clear), sha256 memoization,
key determinism, and the run_r2 integration semantics: hit on same bytes,
automatic miss on changed bytes, no error caching, refresh overwrite,
cache=False bypass.

The r2 integration tests compile tiny C binaries on the fly; each binary gets
a distinct return constant so the compiled bytes differ across cases (gcc
strips comments — source-comment markers do NOT change the binary, a lesson
from the first smoke run where two "different" sources produced identical
bytes and legitimately cache-hit).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from utils.snapshot_cache import (
    SnapshotStore,
    build_cache_key,
    sha256_of_file,
)


@pytest.fixture
def store(tmp_path):
    s = SnapshotStore(db_path=str(tmp_path / "snap.db"))
    yield s
    s.close()


class TestStoreContract:
    def test_put_and_lookup_roundtrip(self, store):
        key = build_cache_key("content", "op")
        assert store.put(key, "content", "op", "sig", {"status": "ok", "v": 1})
        got = store.lookup(key)
        assert got == {"status": "ok", "v": 1}

    def test_lookup_miss_returns_none(self, store):
        assert store.lookup("no-such-key") is None

    def test_lookup_bumps_hits(self, store):
        key = build_cache_key("c", "o")
        store.put(key, "c", "o", "s", {"a": 1})
        store.lookup(key)
        store.lookup(key)
        stats = store.stats()
        assert stats["hits"] >= 2
        assert stats["entries"] >= 1

    def test_put_replaces_same_key(self, store):
        key = build_cache_key("c", "o")
        store.put(key, "c", "o", "s", {"v": 1})
        store.put(key, "c", "o", "s", {"v": 2})
        assert store.lookup(key) == {"v": 2}
        assert store.stats()["entries"] == 1

    def test_per_target_cap_evicts_oldest(self, store, monkeypatch):
        import utils.snapshot_cache as sc
        monkeypatch.setattr(sc, "_MAX_ENTRIES_PER_TARGET", 3)
        # Five ops against the SAME target content — the "explore one binary
        # with many verbs" case the cap exists for.  Distinct op keys so
        # entries don't overwrite each other.
        for i in range(5):
            key = build_cache_key("same-binary", f"op{i}")
            store.put(key, "same-binary", f"op{i}", "s", {"op": i})
        assert store.lookup(build_cache_key("same-binary", "op0")) is None
        assert store.lookup(build_cache_key("same-binary", "op1")) is None
        assert store.lookup(build_cache_key("same-binary", "op4")) == {"op": 4}
        assert store.stats()["entries"] == 3

    def test_purge_expired(self, store, monkeypatch):
        import utils.snapshot_cache as sc
        monkeypatch.setattr(sc, "_TTL_HOURS", 0.0)  # everything is expired
        key = build_cache_key("c", "o")
        store.put(key, "c", "o", "s", {"a": 1})
        assert store.purge_expired() >= 1
        assert store.lookup(key) is None

    def test_clear(self, store):
        for i in range(3):
            store.put(build_cache_key(f"c{i}", "o"), f"c{i}", "o", "s", {})
        assert store.clear() == 3
        assert store.stats()["entries"] == 0


class TestKeysAndHashing:
    def test_key_deterministic_and_content_separated(self):
        k1 = build_cache_key("bin-A", "op1")
        k2 = build_cache_key("bin-A", "op2")
        k3 = build_cache_key("bin-B", "op1")
        assert k1 == build_cache_key("bin-A", "op1")  # deterministic
        assert k1 != k2 and k1 != k3

    def test_sha256_of_file(self, tmp_path):
        f = tmp_path / "x.bin"
        f.write_bytes(b"hello world")
        d1 = sha256_of_file(str(f))
        d2 = sha256_of_file(str(f))  # memoized path
        assert d1 == d2
        assert len(d1) == 64

    def test_sha256_changes_when_bytes_change(self, tmp_path):
        f = tmp_path / "x.bin"
        f.write_bytes(b"v1")
        d1 = sha256_of_file(str(f))
        time.sleep(0.01)
        f.write_bytes(b"v2")
        d2 = sha256_of_file(str(f))
        assert d1 != d2

    def test_sha256_missing_file_none(self, tmp_path):
        assert sha256_of_file(str(tmp_path / "nope")) is None


# ---------------------------------------------------------------------------
# run_r2 integration
# ---------------------------------------------------------------------------
def _gcc_available() -> bool:
    import shutil
    return shutil.which("gcc") is not None


def _build(src: str, out: str) -> None:
    subprocess.run(["gcc", "-o", out, src], check=True, capture_output=True)


@pytest.fixture(scope="module")
def r2_env(tmp_path_factory):
    """Compile two distinct binaries; skip the module when gcc is absent."""
    if not _gcc_available():
        pytest.skip("gcc not available")
    d = tmp_path_factory.mktemp("r2snap")
    src_a = d / "a.c"
    src_a.write_text(textwrap.dedent("""
        #include <stdio.h>
        int secret_menu(void) { return 0x5a; }
        int main(void) { printf("hi"); return secret_menu(); }
    """))
    src_b = d / "b.c"
    src_b.write_text(textwrap.dedent("""
        #include <stdio.h>
        int secret_menu(void) { return 0x77; }
        int main(void) { printf("hi"); return secret_menu(); }
    """))
    bin_a, bin_b = str(d / "bin_a"), str(d / "bin_b")
    _build(str(src_a), bin_a)
    _build(str(src_b), bin_b)
    return {"a": bin_a, "b": bin_b}


@pytest.mark.skipif(not _gcc_available(), reason="gcc not available")
class TestR2SnapshotIntegration:
    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path, monkeypatch):
        """Point the snapshot singleton at a tmp DB and clear the sha memo so
        these tests never collide with persisted run/analysis_snapshots.db or
        memoized digests from other tests."""
        import utils.snapshot_cache as sc
        import auxiliaries.radare2 as r2mod
        store = SnapshotStore(db_path=str(tmp_path / "snap.db"))
        monkeypatch.setattr(sc, "_store", store)
        monkeypatch.setattr(r2mod, "_OUTPUT_CAP", 1024)  # distinct op_key
        sc._sha_memo.clear()
        yield
        store.close()

    def test_fresh_then_hit(self, r2_env):
        from auxiliaries.radare2 import run_r2
        r1 = run_r2(r2_env["a"], "afl")
        assert r1["status"] == "ok"
        assert r1["_snapshot"]["hit"] is False
        r2 = run_r2(r2_env["a"], "afl")
        assert r2["_snapshot"]["hit"] is True
        assert r2["output"] == r1["output"]
        assert r2["exit_code"] == r1["exit_code"]
        assert r2["delta"] == r1["delta"]

    def test_different_bytes_miss(self, r2_env):
        from auxiliaries.radare2 import run_r2
        # bin_a was stored by the previous test in the shared DB (or not, if
        # tests ran independently) — either way bin_b's distinct bytes must
        # miss bin_a's entry for the same verb.
        r_a = run_r2(r2_env["a"], "iI")
        r_b = run_r2(r2_env["b"], "iI")
        assert r_a["target"] != r_b["target"]
        assert r_b["_snapshot"]["hit"] is False or r_a["_snapshot"]["hit"] is True

    def test_error_never_cached(self, r2_env):
        from auxiliaries.radare2 import run_r2
        r1 = run_r2(r2_env["a"], "not_a_verb")
        assert r1["status"] == "error"
        r2 = run_r2(r2_env["a"], "not_a_verb")
        assert r2["status"] == "error"
        assert "_snapshot" not in r2

    def test_refresh_forces_recompute(self, r2_env):
        from auxiliaries.radare2 import run_r2
        rf = run_r2(r2_env["a"], "afl", refresh=True)
        assert rf["_snapshot"]["hit"] is False
        assert rf["status"] == "ok"
        # and the recomputed result is now what's served
        r = run_r2(r2_env["a"], "afl")
        assert r["_snapshot"]["hit"] is True
        assert r["output"] == rf["output"]

    def test_cache_false_bypasses_both(self, r2_env):
        from auxiliaries.radare2 import run_r2
        r = run_r2(r2_env["a"], "iS", cache=False)
        assert "_snapshot" not in r
        assert r["status"] == "ok"

    def test_snapshot_note_present_on_hit(self, r2_env):
        from auxiliaries.radare2 import run_r2
        run_r2(r2_env["a"], "afl", refresh=True)
        r = run_r2(r2_env["a"], "afl")
        assert "refresh=true" in r["_snapshot"]["note"]