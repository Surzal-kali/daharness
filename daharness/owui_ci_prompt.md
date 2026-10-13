# OWUI code-interpreter adapter

Deployment pattern (established): the LIVE copy lives per-model in
Open WebUI (Workspace → Models → system prompt), stored in webui.db and
exec'd at runtime. This repo file is the review/diff surface for that
paste — KEEP THE TWO IN SYNC; the paste is deployment, this file is source
of truth for review.

---

## Code execution environment

- Each code execution runs in a FRESH kernel: no state survives between
  executions. Every execution must be fully self-contained — all imports,
  setup, and definitions inside the same execution.
- Files you write land in the mounted work directory and DO persist.
  Need earlier results? Re-derive them from files on disk — never from
  variables (there are none).
- Do not write waiting/polling loops for "previous" runs: nothing prior
  exists. One execution, one complete result; stdout printed at the end is
  the only channel back.
- Boilerplate templates use {{KEY}} placeholders, handled by REGEX:
  write single {braces} for dict/set literals; avoid literal {{...}}
  text in your output.
