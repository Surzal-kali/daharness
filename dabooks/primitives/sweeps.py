"""sweeps: pure-logic demo sweep envelope for the kernel lane.

PHASE-0 PRIMITIVE — NO playwright, NO network, NO framework imports.
This module fabricates a *deep nested* envelope shaped like what the
framework's nmap/envelope sweep tools produce, so the kernelgate parse
layer can be proven in a cell with zero target IO.

The real sweep stays an existing framework tool (brief §2: "consume
envelopes; never reimplement"). This is only a fixture for lane-side
testing, clearly labeled as synthetic data via ``meta.scanner="synthetic"``
on a TEST-NET-1 host that can never be routed.
"""

from __future__ import annotations

from typing import Any, Dict as TDict, List

# Deeply nested on purpose — 3+ layers, mixed lists/dicts, so the
# first(pluck(env, K)) habit and defensive parsing get exercised the way
# real envelopes do. Fixture host 192.0.2.10 = TEST-NET-1, never routed.
DEMO_SWEEP_ENVELOPE: TDict[str, Any] = {
    "status": "Success",
    "result": {
        "sweep_id": "demo-0001",
        "target": "192.0.2.10",
        "meta": {"scanner": "synthetic", "ports_scanned": [80, 443, 22, 445]},
        "services": [
            {
                "port": 80,
                "protocol": "tcp",
                "service": "http",
                "banner": {"product": "nginx", "version": "1.18.0"},
                "http": {
                    "title": "Index of /",
                    "routes": ["/", "/admin/", "/static/app.js"],
                    "headers": {"Server": "nginx/1.18.0"},
                },
            },
            {
                "port": 22,
                "protocol": "tcp",
                "service": "ssh",
                "banner": {"raw": "SSH-2.0-OpenSSH_8.4p1"},
            },
        ],
        "notes": ["synthetic fixture — no target IO performed"],
    },
    "_elapsed_s": 0.0,
}


def demo_envelope() -> TDict[str, Any]:
    """Return a fresh deep copy of the synthetic sweep envelope."""
    import copy
    return copy.deepcopy(DEMO_SWEEP_ENVELOPE)


def summarize_parsed(env: TDict[str, Any]) -> List[str]:
    """One human line per service (port → service). Pure function: same
    envelope in ⇒ same lines out. Uses kernelgate's fail-closed first()
    so malformed/empty envelopes degrade to 'unknown', never IndexError."""
    from dabooks.primitives.kernelgate import first, pluck

    return [
        f"port {first([p], 'port') or '?'} → {first([s], 'service') or 'unknown'}"
        for p, s in zip(pluck(env, "port"), pluck(env, "service"))
    ]