"""dabooks.primitives — kernel-lane ONLY, deliberately outside the tool registry.

This package is the **jupyter primitives** shim from the build brief in
`primitives.txt`: kernel-side building blocks (rendering, session lifecycle,
event forwarding) for the notebook lane. It exists in `dabooks/` rather than
the framework's tool roots on purpose.

## Why it lives here (architectural)

- `daharness.registry.discover_local_tools` only scans `ALLOWED_TOOL_ROOTS`
  (`auxiliaries/`, `payloads/`, `listeners/`, `utils/`, `encoders/`).
  `dabooks/` is **not a tool root** → the static/AST pass never mints these
  modules into `LOCAL_FILE` tool manifests.
- `BRAIN_SCAN_DIRS` defaults to `auxiliaries,listeners,payloads` → the Brain
  sidecar's dynamic-import pass (`_startup_scan()`) cannot mint these into
  `@framework_tool` handlers either. There is **no** module-level
  `@framework_tool` decorateur in this package, and there never should be.

## The graduation seam (design contract)

These primitives must never mutate `session_manager.py`, `findings.py`, or
`_findings.md` state. **The moment a primitive's *behavior* is invoked by a
model** (OWUI/MCP/secretary), that behavior graduates to a framework tool —
re-decorated in the framework tool roots — and inherits the approval +
scope-gate path. Keep the kernel lane and the tool lane at arm's length:

| lane     | actor | gate |
|----------|-------|------|
| kernel   | human | none (cells inherit human authority) |
| framework| model | scope gate + approval loop |

## Contract per brief §5 (hard rules)

- Primitives render and relay events. They never write framework records.
- No synchronous sleeps; reaper/actions/tasks run on the shared asyncio loop.
- Stateless helpers (`kernelgate`, `sweeps`) must not import playwright.
- **Module-level side effects are FORBIDDEN** in this package (the Brain
  startup-scan rule, §5 "no module-level side effects in scanned dirs"). All
  bootstrapping is done inside functions, behind `await boot()`.

## Module map (Phase 0)

| Module            | Status       | Contract |
|-------------------|--------------|----------|
| `kernelgate.py`   | Phase 0      | defensive parse of kernel helpers; FAIL-CLOSED |
| `sweeps.py`       | Phase 0      | pure-logic demo sweep → deep nested envelope, NO playwright |
| `sessions.py`     | Phase 1      | SessionStore + spawn/kill + idle reaper, **imports playwright** |
| `views.py`        | Phase 2 / 2B | anywidget VNC view + proxied-iframe + inspector |
| `selftest.py`     | Phase 0 smoke (standalone, no framework dep) |

## Phase 0 deliverable (the one shipped now)

Phase 0 ships ONLY: dependency smoke, the pure-logic "envelope shape" demo,
and `kernelgate`. The heavy pieces (playwright session module, anywidget
view) are Phase 1/2 and stay OUT — this is a review-gated build."""