-- Harness Postgres: bootstrap — session roles, database, ownership.
-- 001 runs FIRST (docker-entrypoint-initdb.d ordering) against the default
-- $POSTGRES_DB maintenance connection, as the bootstrap superuser.
--
-- Role map (2026-10-19 design with the operator):
--   harness_operator — the human's psql session. Only role that may run
--                      engagement.purge() (the audit-trail eraser).
--   brain_lane       — Brain sidecar dispatch lane (writer).
--   gateway_lane     — API gateway / secretary lane (writer).
--   repl_lane        — Tool REPL / kernels (writer + operator-correction grants).
--
-- All lanes: INSERT/SELECT on the stores; evidence gets UPDATE for status
-- flips. No lane ever holds DELETE on journal/evidence — enforcement runs
-- through owner-executed SECURITY DEFINER functions in 003_functions.sql.

\set pguser `echo ${HARNESS_DB_USER:-harness_app}`
\set pgpass `echo ${HARNESS_DB_PASSWORD:-harness_change_me}`

SELECT format('bootstrapping lane roles as %s ...', current_user) \gexec

CREATE ROLE harness_operator LOGIN;   -- password set at first operator login
ALTER ROLE harness_operator NOSUPERUSER NOSYSTEMDBG;