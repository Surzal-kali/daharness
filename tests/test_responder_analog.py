"""Tests for the capture-only responder analog (listeners/responder_analog.py).

House conventions (mirrors test_evasion_webshape.py / test_web_session.py):
- live-loopback only: 127.0.0.1, ephemeral ports, tiny stdlib servers;
- the scope gate (utils.gated_http.check_scan) is NOT touched here — the
  responder's control surface is the SHAPING_LAB_MODE lab gate, which these
  tests toggle via monkeypatched env;
- the SMB sink is exercised with a REAL impacket client (loopback, denied
  as designed) — the integration proof of the capture path.
"""
from __future__ import annotations

import importlib.util
import os
import socket
import struct
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(reset=False):
    if reset:
        sys.modules.pop("listeners.responder_analog", None)
        for m in list(sys.modules):
            if m.startswith("listeners."):
                sys.modules.pop(m)
    if "listeners.responder_analog" in sys.modules:
        return sys.modules["listeners.responder_analog"]
    listeners_dir = ROOT / "listeners"
    if str(listeners_dir) not in sys.path:
        sys.path.insert(0, str(listeners_dir))
    spec = importlib.util.spec_from_file_location(
        "listeners.responder_analog", str(listeners_dir / "responder_analog.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["listeners.responder_analog"] = mod
    spec.loader.exec_module(mod)
    return mod


ra = _load()

try:
    import impacket.smbconnection  # noqa: F401

    HAVE_IMPACKET = True
except Exception:  # noqa: BLE001
    HAVE_IMPACKET = False


def lab_on(monkeypatch):
    monkeypatch.setenv("SHAPING_LAB_MODE", "1")


# --- name codec -----------------------------------------------------------------

def test_netbios_codec_roundtrip():
    enc = ra.encode_netbios_name("target-pc")
    dec = ra.decode_netbios_name(enc)
    assert dec.startswith("TARGET-PC") and dec.endswith("20"), dec
    # 16 bytes raw = 15-char padded name + suffix byte, first-level encoded
    assert len(enc) == 32


def test_netbios_decode_never_raises():
    assert ra.decode_netbios_name(b"A" * 4) != ""  # junk tolerated
    assert ra.decode_netbios_name("") == ""
    assert ra.decode_netbios_name(b"A" * 4) == ra.decode_netbios_name(b"A" * 4)


# --- LLMNR discipline -----------------------------------------------------------

def _llmnr_query(qid=0x1234, name=b"FILESERVER01", qtype=1):
    enc = (b"\x0c" + name + b"\x00") if name else b"\x00"
    return struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 0) + enc + struct.pack("!HH", qtype, 1)


def test_llmnr_only_answers_A():
    assert ra.build_llmnr_response(_llmnr_query(qtype=28), "10.0.0.9") is None, "AAAA ignored"
    resp = ra.build_llmnr_response(_llmnr_query(), "10.0.0.9")
    assert resp is not None
    assert resp[:2] == b"\x12\x34"                        # id echoed
    assert b"\xc0\x0c" in resp and b"\x0a\x00\x00\x09" in resp  # ptr + 10.0.0.9
    # QR=1, one question, one answer
    _, flags, qd, an, _, _ = struct.unpack("!HHHHHH", resp[:12])
    assert flags & 0x8000 and qd == 1 and an == 1


def test_llmnr_malformed_never_raises():
    for bad in (b"", b"\x01", b"\x00" * 6, struct.pack("!HHHHHH", 1, 0, 0, 0, 0, 0)):
        assert ra.build_llmnr_response(bad, "10.0.0.9") is None


def test_nbns_answer_shape():
    enc = ra.encode_netbios_name("nas-share")
    query = struct.pack("!HHHHHH", 0xBEEF, 0, 1, 0, 0, 0) + b"\x20" + enc \
        + b"\x00" + struct.pack("!HH", 0x20, 1)
    resp = ra.build_nbns_response(query, "10.0.0.9")
    assert resp is not None
    assert resp[:2] == b"\xbe\xef"                        # trn id echoed
    assert query[12:45] in resp                           # question echoed
    assert b"\x0a\x00\x00\x09" in resp                    # NB record IP


# --- NTLMSSP ---------------------------------------------------------------------

def test_type2_challenge_layout():
    chal = bytes.fromhex("0102030405060708")
    t2 = ra.build_ntlmssp_challenge(chal, "WORKGROUP")
    assert t2[:8] == b"NTLMSSP\x00"
    assert struct.unpack_from("<I", t2, 8)[0] == 2
    assert chal in t2
    # extended-security + NTLM flags set so modern clients send NTLMv2
    flags = struct.unpack_from("<I", t2, 20)[0]
    assert flags & 0x00200000 and flags & 0x00080000


def _synthetic_type3(hmac16, blob, user="jane", domain="CORP"):
    def u16(s):
        return s.encode("utf-16le")

    d16, u16v = u16(domain), u16(user)
    nt = hmac16 + blob
    base = 64  # 12(sig+type) + 6 len/max/offset trios; flags at 60, payload 64
    body = (
        struct.pack("<HHI", 0, 0, base)                  # LM (empty)
        + struct.pack("<HHI", len(nt), 0, base + 16)     # NTLMv2 response
        + struct.pack("<HHI", len(d16), 0, base)         # domain
        + struct.pack("<HHI", len(u16v), 0, base + 8)    # user
        + struct.pack("<HHI", 0, 0, base + 16)           # workstation (empty)
        + struct.pack("<HHI", 0, 0, base + 16)           # encrypted random
        + struct.pack("<I", 0x00200000)                  # negotiate flags
    )
    return b"NTLMSSP\x00" + struct.pack("<I", 3) + body + d16 + u16v + nt


def test_type3_parse_to_5600_line():
    chal_hex = "0102030405060708"
    hmac16 = bytes(range(16))
    blob = b"\x01\x01\x00\x00" + os.urandom(8) + b"\x00" * 4 + b"\x02\x00"
    t3 = _synthetic_type3(hmac16, blob, "jane", "CORP")
    auth = ra.parse_ntlmssp_auth(t3, chal_hex)
    assert auth is not None
    assert auth["user"] == "jane" and auth["domain"] == "CORP"
    parts = auth["hash_line"].split(":")
    assert len(parts) == 6, auth["hash_line"]  # user::domain:chal:hmac:blob
    assert parts[0] == "jane" and parts[1] == "" and parts[2] == "CORP"
    assert parts[3] == chal_hex
    assert parts[4] == hmac16.hex() and parts[5] == blob.hex()


def test_type3_parse_refuses_garbage():
    t2 = ra.build_ntlmssp_challenge(b"\x01" * 8)
    assert ra.parse_ntlmssp_auth(t2, "00" * 8) is None  # type 2 in, refused
    assert ra.parse_ntlmssp_auth(b"garbage", "00" * 8) is None


# --- blacklist -------------------------------------------------------------------

def test_blacklist_own_and_wpad(monkeypatch):

    monkeypatch.setattr(ra.os, "uname", lambda: type("U", (), {"nodename": "thisbox"})())
    ok, why = ra._should_poison("thisbox")
    assert not ok
    ok, why = ra._should_poison("THISBOX.SITE.LAB")
    # FQDN does not match the short name — only exact own names are protected;
    # single-label LLMNR queries are the poison lane, documented honestly.
    ok2, _ = ra._should_poison("wpad")
    assert not ok2
    assert ra._should_poison("fileserv")[0]


# --- lab gate ----------------------------------------------------------------

def test_start_refused_without_lab_mode(monkeypatch, tmp_path):
    monkeypatch.delenv("SHAPING_LAB_MODE", raising=False)
    from utils.scope_gate import ScopeGateError

    with pytest.raises(ScopeGateError) as ei:
        ra.responders_start(iface="127.0.0.1")
    assert "SHAPING_LAB_MODE" in str(ei.value)


def test_raw_runner_gated_too(monkeypatch):
    """Belt: even the internal job main refuses without the lab gate —
    no invocation path starts engines gate-free."""
    monkeypatch.delenv("SHAPING_LAB_MODE", raising=False)
    assert ra.run_job_process([]) == 2


def test_local_tool_optout_constant():
    """The raw job main must never mint as a LOCAL_FILE tool (it would be a
    second, gate-bypassing entry beside the decorated responder tools)."""
    reg = sys.modules.get("daharness.registry")
    if reg is None:
        import daharness.registry as reg  # noqa: F811
    prof = reg.ToolRegistry.extract_module_profile(
        reg.ToolRegistry, Path(ROOT / "listeners" / "responder_analog.py"))
    assert prof.get("_no_local_tool") is True
    assert prof["options"]
    assert prof["docstring"]  # embedding text lives on for the decorated tools


# --- live loopback: LLMNR poisoner round trip --------------------------------------

def test_llmnr_engine_answers_loopback():
    seen = []
    eng = ra.PoisonEngine(
        "llmnr", "127.0.0.1", 15535,
        on_query=lambda d, a, n, ok, why: seen.append((n, ok)))
    eng.start()
    try:
        time.sleep(0.05)
        cs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cs.settimeout(2)
        q = struct.pack("!HHHHHH", 0x77, 0, 1, 0, 0, 0) \
            + b"\x06lab-vm\x00" + struct.pack("!HH", 1, 1)  # len byte: 6
        cs.sendto(q, ("127.0.0.1", 15535))
        resp, _ = cs.recvfrom(1500)
    finally:
        eng.stop()
    assert resp[:2] == b"\x00\x77"
    assert b"\xc0\x0c" in resp and socket.inet_aton("127.0.0.1") in resp
    assert seen and seen[0][1] is True and seen[0][0] == "lab-vm"


# --- live loopback: SMB2 sink + REAL impacket client --------------------------------

@pytest.mark.skipif(not HAVE_IMPACKET, reason="impacket not installed")
def test_smb_sink_captures_impacket_login(monkeypatch, tmp_path):
    monkeypatch.setenv("COOKIE_JAR_DB", str(tmp_path / "jar.db"))
    # memory writes go to the real embedded store; stub them for the test
    calls = []
    monkeypatch.setattr(ra, "_store_capture",
                        lambda e: calls.append(dict(e)))
    found = []
    sink = ra.SMB2CaptureSink(
        "127.0.0.1", 14445, on_capture=lambda e: found.append(dict(e)))
    sink.start()
    try:
        time.sleep(0.05)
        import impacket.smbconnection as smbconn_mod
        from impacket.smbconnection import SMBConnection

        c = SMBConnection("*SMBSERVER", "127.0.0.1", "victim-pc", sess_port=14445,
                          preferredDialect=smbconn_mod.SMB2_DIALECT_002)
        with pytest.raises(Exception):
            c.login("jane.doe", "WrongPass1", "CORP")  # denied as designed
    finally:
        time.sleep(0.1)
        sink.stop()
    assert found, "capture must land"
    cap = found[0]
    assert cap["user"] == "jane.doe" and cap["domain"] == "CORP"
    assert cap["proto"] == "smb2" and cap["victim_ip"] == "127.0.0.1"
    parts = cap["hash_line"].split(":")
    assert len(parts) == 6 and len(parts[4]) == 32  # NTLMv2 HMAC, 5600/john-ready
    assert struct.unpack_from("<H", bytes.fromhex(parts[5])[:4], 0)[0] == 0x0101


if __name__ == "__main__":
    passed = failed = 0
    import inspect

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if len(inspect.signature(fn).parameters):
                    print(f"SKIP (needs fixtures): {name}")
                    continue
                fn()
                passed += 1
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed (fixture-dependent tests skipped)")