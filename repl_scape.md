# REPL surface — tool_repl.py IDE layer reference

Verified against tool_repl.py @ 35f7d0e (Oct 10 2026). Companion to ipythontips.md and the playbook.
When to reach for this: any model/user suggestion about REPL keybinds, ghost-text completion,
toolbar/prompt contents, editor escapes, or completion tuning knobs.

## Keybinds

Keybinding registry is merged over IPython's defaults (Ctrl+L, Ctrl+R, Tab, etc. all stay live).

- **Tab** — framework completions. Tier 1: tool aliases after `run_tool(`-style calls; Tier 2:
  tool_id strings after a tool-accepting command. Ghost text (gray inline suggestion) appears
  for single prefix-matches. `tool?` → manifest + params, `tool??` → source.
- **F1** — dump variables. Prints in-cell (uncommitted, parsed from the cell text at cursor)
  and committed globals with name/type/truncated repr. For a model suggesting a next step,
  this is its memory probe: what's live, what's named.
- **F2** — open current cell in $EDITOR (nvim). Save+quit pipes the cell back into IPython.
- **Ctrl+X Ctrl+E** — same as F2, emacs-style chord.
- **F3** — full nvim escape hatch: launches nvim in workspace root (LazyVim, neo-tree, the
  lot). `:qa` returns; all IPython state (sessions, globals, tool wrappers) intact. Distinct
  from F2: F2 = edit one cell, F3 = standalone IDE session.
- **Ctrl+X Ctrl+O** — fire an Ollama-powered FIM completion of the current prompt (see below).
  Ctrl+X is a dedicated prefix; no escape-timing dependency (the ESC-prefixed variant
  "escape c-o" was tried and dropped — fast typing collapsed it to solo-Escape and fired the
  wrong binding; "does both" symptom, Oct 9).

## Bottom toolbar

Left→right: cursor L:C; cell-local vars parsed from the current cell text (max 6 shown,
20-char reprs — `IPYTHON_TOOLBAR_MAX_VARS`/`_REPR_LEN`); globals count; `[SCOPE ARMED]`
when scope gate is armed; `brain:✓` / `brain:✗` when a brain check applies. Newlines:
`IPYTHON_TOOLBAR_NEWLINES=expand|collapse|off` (default expand).

## Right prompt

Inside a known tool call `alias(`, renders the still-missing **required** params (from the
tool manifest), color-coded: missing = amber, partially-supplied = blue, all-supplied = green.
This is the model's freebie guard against sending incomplete calls.

## Ollama inline completion (ghost text, FIM)

- History suggestions: instant, from past commands (IPython 8.32+ NavigableAutoSuggestFromHistory).
- On-demand model completion: **Ctrl+X Ctrl+O** fires a fill-in-middle completion with the
  cursor context; model sees framework tool names/signatures (alias catalog injected into its
  system prompt). Cursor marker `\n<CURSOR>\n`; context window = 4000 chars before / 500 after
  the cursor; debounce 400ms; min prefix 3 chars.
- This IPython path uses Ollama's chat endpoint with a cursor marker, not native FIM. The
  native-FIM Codestral candidate is documented for the nvim/Minuet lane; changing
  `OLLAMA_COMPLETION_MODEL` does not turn this path into a prefix/suffix completion request.
- Knobs (`OLLAMA_COMPLETION_*` env): ENABLED(1), MODEL(falls back to Ollama default),
  TIMEOUT_MS(6000), NUM_PREDICT(400), NUM_CTX(4096), KEEP_ALIVE(5m), TRIGGER_KEY(c-x c-o),
  DEBOUNCE_MS(400). Failure behavior: 5 consecutive failures → suggester goes quiet 15s
  cooldown, request numbering guards against stale responses.
- **Known gotcha**: repo `.env.example` pins `ENABLED=0` and `TIMEOUT=3000` — if completion
  seems dead or slow, check .env before blaming the model; those pins override the computed
  defaults.
- **VRAM co-residency**: the suggester shares the 3090 with the operator model. While
  gemma4:31b is resident, ghost-text requests queue (num_parallel=1); if FIM matters, flip
  models deliberately at phase boundaries (keep_alive delays expiry but does not pin; explicit
  `ollama stop <model>` frees immediately). Local blink sources (path/snippets/history/buffer)
  never touch VRAM — they are always instant.

## Mouse

`IPYTHON_MOUSE` (default 1): click-to-position enabled; drag-to-select requires Shift+drag
(prompt_toolkit captures mouse events). Set 0 to restore native select-only behavior.

## Editor config

`IPYTHON_EDITOR` (default nvim) drives both F2/Ctrl+X Ctrl+E and F3. `IPYTHON_NVIM_ESCAPE`
(default 1) enables/disables the F3 standalone session.

## Onboarding command

Type `ipython` inside the REPL to print the full tour: tool-count, example calls, operator
functions (scope_on/off/status, scope_search, sessions), UI upgrades currently enabled, and
editor escapes. Good first move in a fresh session; also prints which features the .env has
disabled.
