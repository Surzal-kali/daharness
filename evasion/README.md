# evasion/ — contract-readiness lane: wire-shaping + fingerprint policy

Readiness framing: built to contract-grade shapes, exercised on LAB targets
unless a permitting contract governs the surface. This package holds shaping
**policy** + resolvers consumed by scanner/HTTP tools — it never contacts a
target itself. Boundary: everything beyond this layer (network tunneling,
infra rotation, machine-speed traffic morphing) stays operator-programmed
lanes (REPL/notebooks) — model-adjacent never.

## Modules

| Module | Provides | Consumed by |
| --- | --- | --- |
| `shaping.py` | `SCAN_SHAPING_PRESETS` + `resolve()` — nmap wire profiles | `auxiliaries/nmap.py` via `run_nmap(shaping=)` |
| `webshape.py` | `sticky_ua` deterministic per-host UA + `HostFloor` pacing | `auxiliaries/web_session.py` (every `session_*` call) |

## Honest taxonomy (what evasion is NOT)

- `-T` timing presets tune the scanner's own accuracy/speed trade — the full
  sweep pattern still lands on the wire. They are NOT evasion.
- Wire shaping = `--max-rate` / `--scan-delay` (nmap), `rate=` (masscan;
  already clamped by `MASSCAN_MAX_RATE`), `SESSION_HOST_FLOOR` (HTTP).
  These change what an observer actually records.
- `decoy` / `fragment` presets are `lab_only` — refused without
  `SHAPING_LAB_MODE=1` in the root-0600 .env. Both are fingerprint theater
  against modern stacks, and their preset notes say so.
- UA policy: deterministic per-host from a realistic pool — sticky across a
  session (rotating identity mid-session is how authenticated servers get
  suspicious); spread happens ACROSS hosts via hash distribution.
  `SESSION_UA=legacy` restores the old self-identifying marker; a custom
  `SESSION_UA` value fixes identity fleet-wide.

Errors surface as `ScopeGateError("shaping: ...")` — the scope gate is
untouched and always evaluated first.

Measurement caveat: whether these knobs are evasive in practice needs
differential-testing infra (observe detector behavior with vs. without) —
expansions to this package carry that prerequisite.
