---
priority: 20
---

# Daharness kernel — scripting contract (Jupyter/NBI lane)

Code written here executes INSIDE the framework's daharness kernel
(IPython, framework tools preloaded as awaitable callables). When a
pattern below conflicts with textbook Python, the pattern below wins
on this kernel.

## Event-loop discipline (top rule)
- ALL cells share ONE asyncio event loop on ONE kernel process. A blocking
  wait (time.sleep, requests.get, a bare long loop) in ANY cell starves
  EVERY other cell. "The tool looks broken" is very often this.
- Never time.sleep for pacing. Use `await asyncio.sleep(...)` — it takes
  SECONDS (400 = 6m40s), not milliseconds.
- Long waits: fire-and-collect so the cell returns instantly:

      task = asyncio.create_task(job_poller())
      result = await task          # collect in a LATER cell

- Background-task output streams into whichever cell is executing —
  weird mid-run attribution is normal, not corruption.

## Tool calls
- Tools are async callables: `res = await nmap(target=..., options="-Pn -p 22")`.
  Top-level `await` works (autoawait on). NEVER call without await.
- kwargs with python names, never CLI flag strings. Bad kwarg names are
  REJECTED (preflight) — fix the name, don't retry.
- Aliases and dotted ids both work. Unknown tool_id? Resolve with
  manifest_by_id("auxiliaries.nmap.run_nmap"). `tool?` prints the manifest.

## Job pattern (nmap/masscan/ffuf/hydra/sqlmap/john/hashcat)
- `await run_*` returns an envelope with job_id IMMEDIATELY (the scan is
  still running). Poll with the matching `*_status(job_id=...)` until "done".
- Envelope helpers pluck/find/keys return FLAT LISTS of every hit at any
  depth — scalars need [0]:

      jid = pluck(env, "job_id")[0]   # shipping ['c0529167'] as job_id is
                                      # how polls "never finish"

  And RE-pluck job_id inside each poll iteration; envelopes differ per call.
- Polls are command-only: NEVER bundle sleep inside a tool call. Separate
  cells, or `await asyncio.sleep(2)` between polls.

## Session handles & degraded results
- Handles (ssh/smb/msf/listeners) opened with brain.sock healthy are SHARED
  with the agent lane; `sessions()` lists brain-held vs local.
- A result tagged `degraded: true` is socket-fallback output: "no results"
  negatives are VOID until re-run on a healthy socket.
- msf handles are shared regardless of who opened them.

## Shell interop (`!` prefix; verified IPython 9.17)
- `x = !cmd` gives an SList (x.s single string, x.grep(p)). The shell is
  DASH, not bash: no arrays, no process substitution.
- `$var` expands PYTHON namespace first, env second. `{expr}` is spliced
  BEFORE the shell sees the line — quoting does NOT protect braces.
- `$$var` and `!!cmd` are BROKEN in 9.17 — use `\$var` and plain `!` + `.s`.
- Big outputs go through files (`!grep -i open /tmp/x.txt`), not $var.

## Failure reading
- Plausible-but-wrong/empty + `degraded` flag → never conclude absence.
- 60s timeout envelope on a long tool → work continues brain-side; check
  the matching `*_status` tool, don't treat it as failure.
- Wrong-meaning strings pass preflight CLEANLY: absurd output (3-line crack
  against a 10M wordlist) = suspect ARG SEMANTICS before suspecting the tool.

## NBI lane specifics
- Your run_cell executes with auto_approve — there is NO human confirmation
  mid-chain. Destructive steps must be announced in prose before the cell.
- Framework MCP schemas ride per-request; call attached MCP tools by exact
  dotted ids. Never attach framework MCP in cloud-brained chats (standing
  rule: cloud brains get terminal + web only).
