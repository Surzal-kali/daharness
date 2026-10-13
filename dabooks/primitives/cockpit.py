"""cockpit: kernel-lane sweep→cockpit glue (Phase 3).

A KERNEL MACRO per the tier table: orchestrates the existing tiers
(framework sweep tool envelopes → sessions/views/proxy), holds no
playwright, no framework imports. Pure planning is unit-testable; the
execution half is thin and lives in cells.

## Contract (primitives.txt §4 Phase 3)
- parse deep sweep envelopes DEFENSIVELY ([0] scalars via kernelgate).
- HTTP/S → VNC or proxied view ; banner protocols → status cards
  (host:port, banner, TLS subject/notAfter if cheap).
- ONLY the session module touches targets: banner probes + TLS peeks live
  in sessions.py (the lane's target-IO module) — this file plans and renders.

## Envelope shapes GROUNDED IN SOURCE (2026-10-13)
- nmap (auxiliaries/nmap.py via poll_job verdict): open_ports is a list of
  ``"port/proto/state/service"`` strings (e.g. ``"80/tcp/open/http"``),
  host_state "up"/"down". NOT nested JSON like the Phase-0 demo fixture.
- masscan (auxiliaries/masscan.py): hosts: [{ip, ports: [{port, state,
  ...}], open_count}], open_port_count.
- Any envelope may arrive transport-wrapped ({"status","result"}) — kernelgate
  unwraps; parse here tolerates all three arrival forms (nmap/masscan/demo).
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional, Tuple

# --- lane classification ------------------------------------------------
VIEW_SERVICES = {"http", "https", "http-proxy", "http-alt"}
CARD_SERVICES = {
    "ssh", "ftp", "smtp", "smb", "netbios-ssn", "microsoft-ds",  # ssh/smb/ftp
    "imap", "pop3", "mysql", "postgresql", "redis", "telnet",
}


def classify(service: str) -> str:
    """'view' (browser lane) | 'card' (banner lane) | 'none'."""
    s = (service or "").lower()
    if s in VIEW_SERVICES:
        return "view"
    if s in CARD_SERVICES:
        return "card"
    if s.startswith("ssl/") or "/https" in s:
        return "view"
    return "none"


# --- envelope parsing (defensive, fail-closed) ---------------------------
def _scalar(val: Any, what: str = "value") -> Optional[Any]:
    """Fail-closed scalar unwrapping without kernel imports (pure function):
    list/str scalars → their [0]; empty → None."""
    from dabooks.primitives.kernelgate import first
    if isinstance(val, list):
        return first(val, what)
    return val


def _parse_nmap_ports(env: Any) -> List[Dict[str, Any]]:
    """nmap verdict form: open_ports as 'port/proto/state/service' strings.
    Handles both '80/tcp/open/http' and nmap's native '80/open/tcp//http///'
    (the -oN line the legacy parser captured differently).
    NOTE pluck ground truth: list values come back WHOLE (one entry = the
    outer list) — so iterate sub-lists too (hit live via shape-fire)."""
    from dabooks.primitives.kernelgate import pluck
    out: List[Dict[str, Any]] = []
    for entry in pluck(env, "open_ports"):
        items = entry if isinstance(entry, (list, tuple)) else [entry]
        for item in items:
            if not isinstance(item, str):
                continue
            # form A: port/proto/state/service (nmap.py's verdict parser)
            parts = item.split("/")
            if len(parts) >= 4 and parts[1] in ("tcp", "udp"):
                port, proto, state, service = parts[0], parts[1], parts[2], "/".join(parts[3:])
                out.append({"port": int(port), "proto": proto,
                            "state": state, "service": service, "source": "nmap"})
                continue
            # form B: nmap native '80/open/tcp//http///'
            m = _parse_native_port_line(item)
            if m:
                out.append({**m, "source": "nmap"})
    return out


def _parse_native_port_line(line: str) -> Optional[Dict[str, Any]]:
    import re
    # nmap native: '80/open/tcp//http///' — port/state/proto//service
    m = re.match(r"^(\d+)/(\w+)/(\w+)//([\w?-]+)", line.strip())
    if not m:
        return None
    port, state, proto, service = m.groups()
    return {"port": int(port), "proto": proto, "state": state,
            "service": service if service != "?" else "unknown"}


def _parse_masscan_hosts(env: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """masscan verdict form: hosts[{ip, ports[{port,state,service?}]}]."""
    from dabooks.primitives.kernelgate import find
    services: List[Dict[str, Any]] = []
    target = ""
    ips = find(env, "ip")  # bare-value form: list of ip strings
    if ips and isinstance(ips[0], str):
        target = ips[0]
    for h in find(env, "ports"):
        if not isinstance(h, list):
            continue
        for p in h:
            if not isinstance(p, dict):
                continue
            port = p.get("port")
            if port is None:
                continue
            services.append({
                "port": int(port), "proto": "tcp",
                "state": str(p.get("state", "open")),
                "service": str(p.get("service", p.get("banner", "") or "unknown")),
                "source": "masscan",
            })
    return target, services


def _services_from_demo(env: Any) -> List[Dict[str, Any]]:
    """Phase-0 demo-fixture form: services[{port, service, ...}].
    GROUND-TRUTH TRAP (hit live): kernelgate.find(key, value=None) does NOT
    mean "no value filter" — _MISSING-sentinel semantics; None is a REAL
    filter value that matches nothing here and silently empties the result.
    So: walk 'services' lists directly and read sibling keys off the dicts."""
    from dabooks.primitives.kernelgate import find
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for svc_list in find(env, "services"):
        if not isinstance(svc_list, (list, tuple)):
            continue
        for svc in svc_list:
            if not isinstance(svc, dict):
                continue
            name = svc.get("service")
            port = svc.get("port")
            if not isinstance(port, int):
                continue
            key = (port, name)
            if key in seen:
                continue
            seen.add(key)
            if isinstance(name, str) and name.lower() in (VIEW_SERVICES | CARD_SERVICES):
                out.append({"port": port, "proto": "tcp", "state": "open",
                            "service": name.lower(), "source": "demo"})
    return out


def parse_sweep(envelope: Any) -> Dict[str, Any]:
    """Envelope → {'target', 'host_state', 'services': [...], 'shape'}.
    Defensive: handles transport-wrapped/naked envelopes, nmap/masscan/demo
    shapes, missing fields (fail-closed to empty). Never raises."""
    from dabooks.primitives.kernelgate import keys, pluck
    out: Dict[str, Any] = {"target": None, "host_state": None,
                           "services": [], "shape": "unknown"}

    # transport unwrap happens inside kernelgate pluck/find already
    ks = set(keys(envelope))

    # target: multiple envelope dialects name it differently
    for k in ("target", "ip", "host"):
        if k in ks:
            t = _scalar(pluck(envelope, k)[0] if pluck(envelope, k) else None, k)
            if isinstance(t, str) and t:
                out["target"] = t
                break

    if "open_ports" in ks:  # nmap verdict
        out["services"] = _parse_nmap_ports(envelope)
        hs = pluck(envelope, "host_state")
        out["host_state"] = _scalar(hs[0] if hs else None, "host_state")
        out["shape"] = "nmap"
        return out

    if "hosts" in ks and "open_port_count" in ks:  # masscan verdict
        target, services = _parse_masscan_hosts(envelope)
        if out["target"] is None:
            out["target"] = target or None
        out["services"] = sorted(services, key=lambda s: s["port"])
        out["shape"] = "masscan"
        return out

    if "services" in ks:  # demo fixture
        out["services"] = _services_from_demo(envelope)
        out["shape"] = "demo"
        return out

    return out  # unknown shape — empty services, never raises


# --- banner probes (IO lives in sessions; cockpit only calls) ------------
CARD_PORTS = {"ssh": 22, "ftp": 21, "smtp": 25, "mysql": 3306, "postgresql": 5432}


async def cards(store, services: List[Dict[str, Any]],
                target: Optional[str] = None,
                timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Build banner status cards (phase 3). REAL probes: the lane's
    session-module banner_probe (sessions.py) does the raw-socket work;
    store-level absence is a PROGRAMMING ERROR here, not a silent empty
    (the live 'crossed at 0s' bug was exactly this silent no-op).
    host: the sweep target — cards probe the target, not random hosts.
    FAN-OUT (gather): silent-by-protocol ports (smb/netbios) hold their
    read window whole — SERIAL awaits stacked 4×2s of cell silence and
    read as 'broken' (measured on OWASP-BWA, 2026-10-13). All probes run
    concurrently; wall time ≈ slowest probe, not the sum."""
    import asyncio
    seen: dict = {}  # key → svc (ordered, deduped)
    for svc in services:
        key = (svc.get("port"), svc.get("service"))
        if key not in seen:
            seen[key] = svc
    ordered = list(seen.values())
    if not hasattr(store, "banner_probe"):
        raise AttributeError(
            "cockpit.cards: store lacks banner_probe — import the lane "
            "session module fresh (sessions.get_store()); the silent "
            "no-op here was the '∅ at 0s' bug")

    async def _probe(svc):
        try:
            return await store.banner_probe(svc["port"], host=target, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — a dead probe is a card note
            return f"({type(exc).__name__})"

    banners = await asyncio.gather(*[_probe(s) for s in ordered])
    results: List[Dict[str, Any]] = []
    for svc, banner in zip(ordered, banners):
        card = dict(svc)
        card["banner"] = banner
        if target:
            card["host"] = target  # cards must be attributed (render shows it)
        card["tls"] = None
        results.append(card)
    return results


def render_cards(cards: List[Dict[str, Any]]) -> str:
    """Plain-mode-first status cards: one compact line per service.
    Empty banner reads differentiate protocol silence from blockers."""
    lines = []
    for c in cards:
        banner = (c.get("banner") or "").replace("\n", " ⏎ ")[:60]
        tls = c.get("tls")
        tls_note = f" · tls={tls['subject']} expires {tls['notAfter']}" if tls else ""
        lines.append(f"{c.get('host', '?')}:{c['port']}/{c.get('proto','tcp')} "
                     f"[{c.get('service','?')}] banner={banner or '∅'}{tls_note}")
    return "\n".join(lines) or "(no card services)"


# --- the plan (pure) + execution helpers ---------------------------------
def plan(envelope: Any, target: Optional[str] = None) -> Dict[str, Any]:
    """Pure: parsed sweep → ordered execution plan for the cockpit cell:
    {'target', 'shape', 'views': [svc...], 'cards': [svc...]}. Views in
    ascending port; cards deduped by (port, service).
    ``target`` override for verdict shapes that carry no host identity
    (nmap verdicts name the target nowhere — the operator knows theirs)."""
    parsed = parse_sweep(envelope)
    if target:
        parsed["target"] = target
    views: List[Dict[str, Any]] = []
    card_svcs: List[Dict[str, Any]] = []
    for svc in parsed["services"]:
        lane = classify(str(svc.get("service", "")))
        if lane == "view" and svc.get("state") == "open":
            views.append(svc)
        elif lane == "card" and svc.get("state") == "open":
            card_svcs.append(svc)
    return {"target": parsed["target"], "shape": parsed["shape"],
            "views": views, "cards": card_svcs}