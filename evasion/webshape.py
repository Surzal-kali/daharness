"""Web-session fingerprint lacing + host-floor pacing (evasion L3).

Consumed by auxiliaries/web_session (single choke points: _merge_headers for
UA, _do_session_request for pacing). Readiness framing (user ruling Oct 17):
exercise on lab targets unless a permitting contract exists.

NO ROTATION-WITHIN-HOST by contract: UA is DETERMINISTIC-PER-HOST — the same
host always sees the same agent, across processes and restarts (pure hash,
zero state). Rotating identity WITHIN a session is how authenticated servers
get suspicious; spread happens ACROSS hosts via hash distribution. Operators
who genuinely want rotation own it in REPL/notebook code, not here.
"""
from __future__ import annotations

import hashlib
import os

# Stable major-version strings — deliberately NOT bleeding-edge: a UA claiming
# a browser build newer than what its implied TLS stack supports tells a
# fingerprint story that contradicts itself.
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# The old self-identifying marker, kept as an opt-in via SESSION_UA=legacy
# (some RoE documents require the tool to identify itself).
LEGACY_UA = "framework-web-session/1.0 (+stateful-audit-lane)"


def sticky_ua(host: str) -> str:
    """Deterministic per-host UA from the realistic pool.

    Precedence: $SESSION_UA override ('legacy' | custom string) > hash-pick.
    """
    if not host:
        return _UA_POOL[0]
    override = os.getenv("SESSION_UA", "").strip()
    if override == "legacy" or override == LEGACY_UA:
        return LEGACY_UA
    if override:
        return override
    h = hashlib.sha256(host.lower().encode("utf-8")).digest()
    return _UA_POOL[h[0] % len(_UA_POOL)]


def host_floor_seconds() -> float:
    """Configured per-host floor interval (seconds); 0.0 = off (default).

    Read from $SESSION_HOST_FLOOR (float ok). Clamped to <=15s: the tool
    lanes are blocking-with-timeout; a floor longer than that belongs in
    REPL/notebook code, not in the request path.
    """
    raw = os.getenv("SESSION_HOST_FLOOR", "").strip()
    if not raw:
        return 0.0
    try:
        v = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, min(v, 15.0))


class HostFloor:
    """Per-host inter-request floor at the request choke point.

    Monotonic in-process timestamps. Deliberately NO locks: a floor is
    pacing, not correctness — in multi-process lanes two racers can both
    pass the check, and advisory best-effort is the honest failure mode.
    """

    _at: dict = {}

    @classmethod
    def wait(cls, host: str) -> float:
        """Sleep out the floor for ``host``; returns seconds actually slept."""
        import time

        floor = host_floor_seconds()
        if floor <= 0.0 or not host:
            return 0.0
        now = time.monotonic()
        last = cls._at.get(host)
        if last is not None:
            remaining = floor - (now - last)
            if remaining > 0:
                time.sleep(remaining)
                cls._at[host] = time.monotonic()
                return remaining
        cls._at[host] = now
        return 0.0
