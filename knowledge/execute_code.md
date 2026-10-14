# Execute Code Lane — Jupyter Kernel

## what it is

OpenWebUI is configured with Jupyter as the code interpreter and execution
engine. The `execute_code` lane runs in the same daharness Jupyter kernel
the REPL (`tool_repl.py`) uses — the framework namespace (bare tool names,
async wrappers, envelope helpers) and the scope gate are wired in
identically. This is a DIRECT EXECUTION lane: the model can run multi-step
tool chains programmatically without handing off to the User for REPL entry.

## execution scope — one call = one fresh context

Each `execute_code` invocation is a self-contained script. Variables,
imports, and state created inside one call do NOT persist to the next
`execute_code` call. There is no global variable state across calls. Write
each block as a complete, self-contained script: declare imports, define
helpers, run the chain, and bank results to stores all within the same
block. If a later call needs a value from an earlier call, retrieve it from
stores (findings, memory, scratch) — never assume a variable survived.

## within a single call

Multiple `await` lines in one execute_code block run sequentially and
variables carry forward line-to-line:

```python
result = await nmap(target="10.10.10.50", options="-Pn -p 22,445")
ports = pluck(result, "open_ports")
if 445 in ports:
    await smb_recon(target="10.10.10.50")
```

That entire block is ONE execute_code call. The next execute_code call
starts clean — `result`, `ports`, and any imports are gone.

## tool names

Tool names in execute_code use BASE NAMES (bare tool names the kernel
resolves, not dotted tool_ids): `nmap`, `masscan`, `ffuf`, `smb_null`,
`smb_recon`, `ftp_recon`, `dns_lookup`, `tls_info`, `cors_probe`,
`ssh_exec`, `web_probe`, `db_client`, `hydra`, `sqlmap`, `hash_crack`,
`searchsploit`, `msfvenom`, `metasploit`, `session_get`, `session_post`,
`session_upload`. The full alias set is in `tool_repl.py:_TOOL_ALIASES` +
auto-derived from module_id.

## await is required

Every framework tool call in execute_code is `await`ed. The kernel injects
async wrappers (not the raw functions) and autoawait is on. Without `await`
you get a coroutine object, not the result, and the tool never executes.

## transport envelope

Results come back in a transport envelope:
`{"status": ..., "result": <tool dict>, "_elapsed_s": N}`. The tool's own
fields are under `result`, not at the top level. The kernel namespace ships
envelope helpers that auto-unwrap: `keys(r)` (every key at any depth),
`pluck(r, K)` (flat list of every value for key K), `find(r, K)` (values +
paths), `grep(r, RX)` (string search across all values). Use these instead
of guessing the nesting.

## scope gate

The scope gate fires on every traffic-sending call inside the kernel — it
enforces identically to the bridge. The kernel does not bypass the scope
gate. No manual [GATED] markers needed: the gate is automatic.

## budget

The per-turn tool budget (`TOOL_BUDGET_PER_TURN`) is enforced on the bridge
wrapper (`framework_run_tool` / OWUI tool calls), NOT on execute_code. The
kernel talks to the framework directly, so chains run unbounded. This is
deliberate: scripting loops and multi-step chains through execute_code is
the expected workflow, and a per-call cap would make that impossible.