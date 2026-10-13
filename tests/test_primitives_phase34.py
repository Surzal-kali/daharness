"""Phase 3+4 tests: cockpit parser (all grounded envelope dialects),
banner cards (fake store), and the chapter writer. No chromium, no network.
"""

import asyncio
import json

import pytest

from dabooks.primitives.cockpit import (
    classify, parse_sweep, plan, render_cards, _parse_native_port_line,
)
from dabooks.primitives.sweeps import demo_envelope


def _run(coro):
    return asyncio.run(coro)


# grounded envelope fixtures (shapes from auxiliaries/nmap.py + masscan.py)
NMAP_ENV = {"status": "Success", "result": {
    "job_id": "nmap-1", "status": "done", "host_state": "up",
    "open_ports": ["80/tcp/open/http", "22/tcp/open/ssh",
                   "443/tcp/open/https", "445/tcp/open/microsoft-ds"],
    "elapsed": 4.2}}
NMAP_NATIVE_ENV = {"status": "Success", "result": {
    "open_ports": ["80/open/tcp//http///", "22/open/tcp//ssh///"]}}
MASSCAN_ENV = {"status": "Success", "result": {
    "open_port_count": 2, "hosts_with_open": 1,
    "hosts": [{"ip": "192.168.56.107", "ports": [
        {"port": 80, "state": "open"}, {"port": 22, "state": "open"}]}]}}


# --- classify ------------------------------------------------------------
def test_classify_lanes():
    assert classify("http") == "view"
    assert classify("https") == "view"
    assert classify("http-alt") == "view"
    assert classify("ssl/https") == "view"
    assert classify("ssh") == "card"
    assert classify("ftp") == "card"
    assert classify("smtp") == "card"
    assert classify("microsoft-ds") == "card"
    assert classify("mysql") == "card"
    assert classify("unknown") == "none"
    assert classify("") == "none"


def test_classify_case_insensitive():
    assert classify("HTTP") == "view"
    assert classify("SSH") == "card"


# --- nmap dialect --------------------------------------------------------
def test_parse_nmap_verdict_form():
    p = parse_sweep(NMAP_ENV)
    assert p["shape"] == "nmap"
    assert p["host_state"] == "up"
    ports = {(s["port"], s["service"]) for s in p["services"]}
    assert (80, "http") in ports and (22, "ssh") in ports
    assert (445, "microsoft-ds") in ports
    assert all(s["source"] == "nmap" for s in p["services"])


def test_parse_nmap_native_form():
    p = parse_sweep(NMAP_NATIVE_ENV)
    assert p["shape"] == "nmap"
    ports = {(s["port"], s["service"]) for s in p["services"]}
    assert (80, "http") in ports and (22, "ssh") in ports


def test_plan_nmap_splits_lanes_and_honors_target_override():
    p = plan(NMAP_ENV, target="192.168.56.107")
    assert p["target"] == "192.168.56.107"  # override (verdict has no target)
    assert [(v["port"], v["service"]) for v in p["views"]] == \
        [(80, "http"), (443, "https")]
    assert sorted((c["port"], c["service"]) for c in p["cards"]) == \
        [(22, "ssh"), (445, "microsoft-ds")]


# --- masscan dialect -----------------------------------------------------
def test_parse_masscan_form():
    p = parse_sweep(MASSCAN_ENV)
    assert p["shape"] == "masscan"
    assert p["target"] == "192.168.56.107"
    assert sorted(s["port"] for s in p["services"]) == [22, 80]


def test_masscan_unknown_services_stay_out_of_lanes():
    # masscan reports ports, not service names — 'unknown' classifies 'none';
    # enrichment is the banner probe's job, so plan() yields empty lanes.
    p = plan(MASSCAN_ENV)
    assert p["views"] == [] and p["cards"] == []


# --- demo dialect --------------------------------------------------------
def test_parse_demo_form():
    p = plan(demo_envelope())
    assert p["shape"] == "demo"
    assert [(v["port"], v["service"]) for v in p["views"]] == [(80, "http")]
    assert [(c["port"], c["service"]) for c in p["cards"]] == [(22, "ssh")]


def test_demo_parser_ignores_non_int_ports_and_dupes():
    env = {"status": "ok", "result": {"services": [
        {"port": 80, "service": "http"}, {"port": 80, "service": "http"},
        {"port": "80", "service": "http"},  # string port — skipped
        {"service": "ssh"},                # no port — skipped
    ]}}
    p = parse_sweep(env)
    assert len(p["services"]) == 1  # deduped, only the int-port entry


# --- fail-closed against garbage ----------------------------------------
@pytest.mark.parametrize("bad", [None, {}, {"status": "ok"},
                                 {"result": {"nonsense": 1}}, "a string", 42])
def test_parse_sweep_never_raises(bad):
    out = parse_sweep(bad)
    assert out["services"] == []


def test_pluck_whole_list_trap_is_handled():
    # pluck returns list values WHOLE (ground truth); the nmap parser must
    # iterate sub-lists — this test would fail on the naive reader.
    p = parse_sweep(NMAP_ENV)
    assert len(p["services"]) == 4


def test_find_value_none_is_a_filter_not_wildcard():
    # ground truth: find(key, value=None) matches only literal null —
    # a naive 'value=None means no filter' call silently empties. The demo
    # parser must NOT pass value=None (documented + fixed live).
    from dabooks.primitives.kernelgate import find
    assert find({"result": {"port": 80}}, "port", value=None) == []


# --- native line parser directly ----------------------------------------
def test_native_line_group_order():
    rec = _parse_native_port_line("80/open/tcp//http///")
    assert rec == {"port": 80, "proto": "tcp", "state": "open",
                   "service": "http"}


def test_native_line_unknown_service():
    rec = _parse_native_port_line("8080/open/tcp//?///")
    assert rec["service"] == "unknown"


# --- banner cards (fake store) -------------------------------------------
class FakeStore:
    async def banner_probe(self, port, host=None, timeout=5.0):
        return {22: "SSH-2.0-OpenSSH_8.4p1", 21: "220 ftpd ready"}.get(port, "")


def test_cards_bounded_probe_with_fake_store():
    async def run():
        from dabooks.primitives.cockpit import cards
        out = await cards(FakeStore(), [{"port": 22, "service": "ssh"}],
                          target="10.0.0.1")
        assert out[0]["banner"] == "SSH-2.0-OpenSSH_8.4p1"
        # host now rides the card (the 0s-∅ bug's sibling: unattributed rows)
        assert out[0]["host"] == "10.0.0.1"
    _run(run())


def test_cards_missing_banner_probe_raises_loudly():
    """The silent no-op was the '∅ at 0s' live bug — absence is now an
    explicit programming error, not an empty result."""
    async def run():
        from dabooks.primitives.cockpit import cards
        with pytest.raises(AttributeError, match="banner_probe"):
            await cards(object(), [{"port": 22, "service": "ssh"}])
    _run(run())


def test_render_cards_plain_text():
    line = render_cards([{"port": 22, "proto": "tcp", "service": "ssh",
                          "banner": "SSH-2.0-OpenSSH_8.4p1", "tls": None}])
    assert "22/tcp" in line and "ssh" in line
    assert "SSH-2.0-OpenSSH_8.4p1" in line


def test_render_cards_empty():
    assert render_cards([]) == "(no card services)"


# --- Phase 4: chapter writer ---------------------------------------------
def test_write_chapter_creates_valid_notebook(tmp_path, monkeypatch):
    import dabooks.primitives.chapters as chapters
    monkeypatch.setenv("COCKPIT_CHAPTER_DIR", str(tmp_path))
    p = plan(NMAP_ENV, target="192.168.56.107")
    out = chapters.write_chapter(NMAP_ENV, p, filename="t.ipynb")
    assert out.exists() and out.name == "t.ipynb"
    nb = json.loads(out.read_text())
    assert nb["nbformat"] == 4
    assert nb["metadata"]["kernelspec"]["name"] == "daharness"
    # count against the RAW FILE text (json.dumps re-escapes '·' as \u00b7)
    src = out.read_text()
    # envelope committed in the leading cells
    assert "192.168.56.107" in src or "open_ports" in src
    # one section per planned service — headers match on service+port pairs
    # ('## http' alone would double-count: the https heading contains it too)
    for header in ("## http · port 80", "## https · port 443",
                   "## ssh · port 22", "## microsoft-ds · port 445"):
        assert src.count(header) == 1, header


def test_write_chapter_slug_and_default_name(tmp_path, monkeypatch):
    import dabooks.primitives.chapters as chapters
    import re
    monkeypatch.setenv("COCKPIT_CHAPTER_DIR", str(tmp_path))
    p = plan(MASSCAN_ENV)  # target in envelope
    out = chapters.write_chapter(NMAP_ENV, p)
    assert out.name.startswith("chapter-192.168.56.107-")
    assert re.search(r"chapter-192\.168\.56\.107-\d{8}-\d{6}\.ipynb$", out.name)


def test_write_chapter_never_git_commits():
    # the lane never invokes git — static source check
    import dabooks.primitives.chapters as chapters
    import inspect
    src = inspect.getsource(chapters)
    assert "subprocess" not in src
    assert "git " not in src.replace("# ", "")


def test_chapter_skeleton_valid_json_nbformat45(tmp_path, monkeypatch):
    import dabooks.primitives.chapters as chapters
    monkeypatch.setenv("COCKPIT_CHAPTER_DIR", str(tmp_path))
    p = {"target": "x", "shape": "demo",
         "views": [{"port": 80, "service": "http", "source": "demo", "state": "open"}],
         "cards": [{"port": 22, "service": "ssh", "source": "demo", "state": "open"}]}
    out = chapters.write_chapter({"demo": True}, p)
    nb = json.loads(out.read_text())
    types = [c["cell_type"] for c in nb["cells"]]
    assert types[0] == "markdown"          # leading cell present
    assert types.count("code") >= 3        # setup + per-service cells


# --- purity --------------------------------------------------------------
def test_cockpit_chapters_no_framework_imports():
    import ast
    from pathlib import Path
    for name in ("cockpit.py", "chapters.py"):
        src = (Path(__file__).resolve().parent.parent / "dabooks"
               / "primitives" / name).read_text()
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for n in names:
                    if n.split(".")[0] in ("daharness", "constants", "utils",
                                           "auxiliaries", "payloads", "listeners"):
                        offenders.append(n)
        assert offenders == [], (name, offenders)