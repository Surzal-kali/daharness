# Findings Lifecycle

## report_finding

`report_finding` (`utils/findings.py`) is the terminal action for any tool
chain. It mints a structured `Finding` in the SQLite findings store
(`ids.db`) and stores a lightweight one-line pointer in vector memory so the
secretary can recall that the finding exists later without bloating its
context with the full evidence payload.

## finding fields

- `title`: Short summary, e.g. "SQL Injection in /login"
- `severity`: P1 (critical), P2 (high), P3 (medium), P4 (info)
- `asset`: The affected host/URL/endpoint
- `cwe`: CWE identifier, e.g. "CWE-89" (optional)
- `evidence`: JSON dict with request, response, excerpt
- `repro`: JSON list of reproduction steps
- `tool_chain`: JSON list of tools used in the chain
- `memory_ref`: One-line vector-memory pointer

## finding IDs

Sequential `F-0001`-style IDs. Concurrent adds (gateway + secretary + MCP
clients all share `ids.db`) can compute the same F-id; the PRIMARY KEY turns
that into a loud IntegrityError instead of a duplicate. The store recounts
and retries so the caller just gets their finding, slightly later. A lost
id race falls back to `F-<uuid4>`.

## lifecycle statuses

Findings start as `open`. Status transitions:
- `open` → `closed` (resolved)
- `open` → `false_positive` (not a real finding)
- `open` → `duplicate` (same as another finding)
- `open` → `superseded` (replaced by a newer finding, `superseded_by` field)

Lifecycle columns (`status`, `superseded_by`, `closed_by`, `closed_reason`,
`closed_ts`) are added by idempotent `ALTER TABLE` migration — see
`FindingStore._init_table`.

## render_findings

`render_findings` reads the findings store and returns a markdown report
suitable as a bug-bounty submission draft or lab documentation. The
secretary can call it mid-session to review what has been found so far.
Filters by severity, asset, and status.

## credential-reuse sweep

Every recovered credential — dumped (secretsdump), cracked (hashcat/john),
decoded (GPP cpassword, Base64 config blobs) — gets ONE validation attempt
against each other reachable service before deeper work continues. This is a
standing step: SSH, web login forms, HTTP Basic, MySQL, PostgreSQL, SMB,
FTP. A single failure means "not reused here" — do not brute-force. Log
each validation via `report_finding` so the chain is auditable. The
`report_finding` tool carries a `next_hints` entry that nudges the
secretary to do this sweep automatically.