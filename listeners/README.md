# listeners/ — Brain sidecar, OOB callbacks, runtime services

The runtime services: the Brain sidecar (the tool dispatch hub), the OOB
collaborator, the TCP listener, and the Brain-side execution kill switch.

## Brain Sidecar (`thebrain.py`)

A Unix domain socket server at the checkout's gitignored `run/brain.sock`
(resolved via `constants.brain_socket_path()`; `$BRAIN_SOCKET` overrides)
with 4-byte big-endian length-prefixed message framing
(`pack_message` / `read_message`). The socket lives **inside the tree** so
bind-mounted container lanes (the jupyter kernel in the workbench, e.g.)
reach the SAME socket as the host — a `/tmp` path would be per-namespace and
unreachable across a container boundary.

**Startup sequence:**

1. Probe for an existing live listener (refuses to start a second Brain).
2. Unlink stale socket if no live listener.
3. `_startup_scan()` — imports modules under `BRAIN_SCAN_DIRS` (default:
   `auxiliaries,listeners,payloads`) and registers `@framework_tool`
   callables in the `FunctionRegistry`.
4. Bind the socket (only after the scan completes, so tools are callable
   immediately when bootstrap's connect-probe succeeds).

**Event protocol:** `event_type|session_id|data`

- `SCAN_TOOLS|sid|path` — scan a directory/file for tools, register them.
- `CALL_TOOL|sid|tool_id|args_json` — execute a registered tool. Args as
  dict → kwargs, list → positional, or bare string → single positional.
- Unknown events → forwarded to the C library (`frameit.so`) via `ctypes`.

**Class instance caching:** `FunctionRegistry._instances` keeps one instance
per class so stateful clients (e.g. `MetasploitClient`, via its
`get_instance()` singleton) preserve handles across calls.

**Timeout semantics:** dispatch waits up to `BRAIN_DISPATCH_TIMEOUT`
(default 600s). If the Brain **accepted** the call but timed out, the result
is a failure and the tool is **NOT retried in-process** — it may still be
running on the Brain, and a second execution would cause duplicate side
effects. `BRAIN_EXEC_CEILING` (default 3600s) hard-caps any single
execution on the Brain.

**Shutdown:** signal handlers route SIGTERM/SIGINT through task cancellation
so the `finally` block unlinks the socket — but only if the inode still
matches the one we bound (won't clobber a newer sidecar).

**Pitfall:** the kernel doesn't unlink the socket when its owner is killed
with SIGKILL. Always probe with a real `connect()`, never `Path.exists()` —
bootstrap's `_brain_ready()` does this.

### In-Process Fallback (`../daharness/executor.py`)

When the Brain socket is unavailable (`FileNotFoundError`,
`ConnectionError`) or doesn't know the tool, `BRAIN_DISPATCH` calls execute
in-process:

1. `_resolve_callable()` — longest importable module prefix wins, walks
   remaining attrs. Unbound methods are bound to a cached class instance,
   preferring `get_instance()` if available.
2. Async tools are awaited; sync tools run in `asyncio.to_thread()`.
3. Results are wrapped as `{"status", "result"}` envelopes; `stdout` is
   included only when the tool returned a bare string, never as a
   stringified copy of `result` (that doubled context on every call).

Process-local caveat: the fallback cannot see Brain-held sessions — see the
Session Manager notes in
[`../utils/README.md`](../utils/README.md#session-manager-session_managerpy).

## Zombie Kill Switch (`brain_control.py` + `execution_tracker.py`)

`execution_tracker` keeps the Brain-side live/tracked-execution state;
`brain_control` exposes it as framework tools — `list_tool_executions` /
`kill_tool_execution` — so wedged tool executions on the Brain can be
inspected and killed without killing the Brain itself.

## OOB Collaborator (`collaborator.py`)

A Burp Collaborator analog: multi-protocol listener catching blind SSRF,
blind XSS, and other OOB callbacks. HTTP on TCP 80, HTTPS on TCP 443
(self-signed cert), and DNS on UDP 53 — all on one host. The payload ID
rides in the subdomain; `collab_generate` produces a unique callback
URL/DNS name and `collab_poll` returns correlated events. Requires root for
ports 80, 443, and 53.

**Public mode** — set `COLLAB_PUBLIC_URL` (e.g. a Tailscale Funnel endpoint
like `https://<device>.<tailnet>.ts.net`) and `collab_generate` returns
path-based PUBLIC callback URLs (`<public>/c/<id>/`) instead of lab
subdomains, and a token-gated redirect endpoint goes live at
`/r/<id>?to=<url>` → 302 (redirect-to-internal blind SSRF; `scan_ssrf`
gains a `redirect_to` param for it). Funnel walkthrough: `tailscale funnel
<COLLAB_HTTP_PORT>` (use 8080 — no root needed), public HTTPS terminates at
Tailscale and forwards plain HTTP to `127.0.0.1:<COLLAB_HTTP_PORT>`. Honest
limits: Funnel serves HTTPS only and does not expose DNS-query events, so
public mode is HTTP-callback-only (subdomain mode keeps the DNS signal).
Every callback is appended to `scope/collab_hits.jsonl` as durable evidence
(gitignored); the in-memory store is capped at 5000 entries.

## TCP Listener (`listening.py`)

`listen()` starts a TCP listener **without blocking the dispatcher**: bind
the socket, hand serving off to a background `asyncio.create_task()`, and
return immediately. This is the required pattern for any service/listener
tool — a `serve_forever()` in a tool would hang its dispatcher.

## Raw SYN Scanner (`raw_scan.py` + `plugins/raw_scan.cpp`)

The scan lane's raw SYN port scanner: a C++ plugin (`SOCK_RAW`; requires
root or `CAP_NET_RAW`) wrapped via `ctypes.CDLL` and exposed as
`@framework_tool` callables — scope-gated like every other traffic-sending
tool.

## C plugins in this folder

- `plugins/raw_scan.cpp` → `raw_scan.so` — raw SYN scan using `SOCK_RAW`
- `plugins/frameit.c` → `frameit.so` — C-side event bridge for the Brain

Compiled with `-fPIC -shared`; the Python wrappers always set `argtypes` and
`restype` explicitly (see the plugins convention in
[`AGENTS.md`](../AGENTS.md#language-choice)).