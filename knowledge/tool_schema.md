# Tool Schema — ids.db

## ids.db overview

One SQLite database file at the workspace root (`ids.db`). It is shared:
`utils/sessions.py` owns `targets` / `sessions` / `notes`, and
`daharness/findings.py` owns `findings`. Concurrent access from the gateway,
secretary, and MCP clients is handled with busy-timeout + retry inside
`daharness/findings.py`.

## lazy creation

`daharness.findings.ensure_ids_db()` creates the file and all four tables on
first call. Bootstrap's `launch_all` runs it as a preflight (before the
wordlist preflight), and `framework_health` calls it so a fresh checkout
reports the DB as up instead of "not found". Idempotent on an existing
database. No manual `sqlite3 ids.db` step needed on a fresh install.

## targets table

Owned by `utils/sessions.py:DatabaseManager`.

| Column     | Type    | Notes                          |
|------------|---------|--------------------------------|
| id         | INTEGER | PRIMARY KEY                    |
| ip_address | TEXT    |                                |
| port       | INTEGER |                                |
| status     | TEXT    | inserted as `'active'`         |

## sessions table

Owned by `utils/sessions.py:DatabaseManager`.

| Column     | Type     | Notes                                   |
|------------|----------|-----------------------------------------|
| id         | INTEGER  | PRIMARY KEY                             |
| target_id  | INTEGER  | FK → targets.id                         |
| payload    | TEXT     | payload name — plain column             |
| status     | TEXT     | inserted as `'active'`                  |
| start_time | DATETIME | CURRENT_TIMESTAMP on insert             |
| end_time   | DATETIME | nullable                                |
| note       | TEXT     | nullable                                |

## notes table

Owned by `utils/sessions.py:DatabaseManager`.

| Column    | Type    | Notes           |
|-----------|---------|-----------------|
| id        | INTEGER | PRIMARY KEY     |
| target_id | INTEGER | FK → targets.id |
| note      | TEXT     |                 |

## findings table

Owned by `daharness/findings.py:FindingStore`.

| Column        | Type | Notes                                                          |
|---------------|------|----------------------------------------------------------------|
| id            | TEXT | PRIMARY KEY — sequential `F-0001`-style id, IntegrityError-safe |
| title         | TEXT | NOT NULL                                                        |
| severity      | TEXT | NOT NULL — P1–P4                                                |
| cwe           | TEXT | optional                                                        |
| asset         | TEXT | NOT NULL                                                        |
| evidence      | TEXT | JSON (request/response/excerpt)                                 |
| repro         | TEXT | JSON list of reproduction steps                                 |
| tool_chain    | TEXT | JSON list of tools used                                         |
| memory_ref    | TEXT | one-line vector-memory pointer                                  |
| ts            | TEXT | NOT NULL — UTC ISO-8601                                         |
| status        | TEXT | NOT NULL DEFAULT `'open'`                                       |
| superseded_by | TEXT | added by migration                                              |
| closed_by     | TEXT | added by migration                                              |
| closed_reason | TEXT | added by migration                                              |
| closed_ts     | TEXT | added by migration                                              |

Migrations: pre-existing tables get lifecycle columns via idempotent
`ALTER TABLE` (column list checked through `PRAGMA table_info`) — see
`FindingStore._init_table`. `DatabaseManager` accepts an optional `db_path`
parameter (defaults to `resolve_workspace_root() / "ids.db"`).

## WAL mode

The findings store opens with `PRAGMA journal_mode=WAL` — readers don't block
the writer and vice versa across processes. Cold-start note: on a brand-new
file every concurrent connect sees non-WAL mode and races on the
journal_mode pragma; the setup retries, only the first process flips the
file into WAL. A root guard (`_repair_ownership_if_needed`) chowns `ids.db`
+ WAL + SHM back to the workspace owner when a prior root run left them
root-owned.