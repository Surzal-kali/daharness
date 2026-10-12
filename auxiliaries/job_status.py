"""Generic background-job status poller — the one-stop shop for job_id lookups.

Every long-running CLI tool in the framework (nmap, masscan, ffuf, hydra,
sqlmap, amass, hashcat, john) launches a detached subprocess via
:mod:`utils.background_job` and returns a random ``job_id`` (8 hex chars).
Each tool has its own ``<tool>_status(job_id)`` wrapper, but the model
often has a bare ``job_id`` without remembering which tool produced it —
especially after a conversation grows long or a session was resumed.

``job_status`` closes that gap: given a ``job_id``, it:

1. Checks the in-process ``_JOBS`` registry (fast path — has the live
   ``verdict_parser`` closure set at launch time).
2. If not found, scans the log dir (``$BG_JOB_LOG_DIR`` / ``/tmp``) for
   ``*_<job_id>.meta`` and reads the ``tool`` field from the sidecar.
3. Looks up the verdict parser from the parser registry (populated at
   Brain startup when tool modules are imported).
4. Calls :func:`poll_job` with the discovered tool name.

This means the model can poll any job by its id without knowing which tool
launched it — no more guessing between ``nmap_status``, ``ffuf_status``,
``hydra_status``, etc.

**sqlmap note:** sqlmap has its own private ``_JOBS`` dict and doesn't use
the shared ``launch_job`` helper.  When ``job_status`` finds a sqlmap
sidecar, it delegates to ``sqlmap_status`` directly (importing it lazily to
avoid a circular import at module load time).

No kernel restart needed: the Brain's startup scan imports this module
(it's in ``auxiliaries/``, which is in ``BRAIN_SCAN_DIRS``), registering
the tool immediately.  A Brain restart (``python bootstrap.py --daemon``)
is all that's required.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from constants import framework_tool
from utils.background_job import (
    _JOBS,
    _JOBS_LOCK,
    _find_job_tool,
    _lookup_verdict_parser,
    poll_job,
)


@framework_tool(
    "Poll the status of ANY background job by its job_id — works for nmap, "
    "masscan, ffuf, hydra, sqlmap, amass, hashcat, john, and any future "
    "tool that uses the background-job pattern. Returns running/done, the "
    "tool name, tool-specific parsed verdict (open ports, cracked creds, "
    "injectable verdict, discovered paths, etc.), recent log lines, and "
    "elapsed time. Use this when you have a job_id but aren't sure which "
    "tool launched it. Call once when the user requests an update; never "
    "poll repeatedly until done.",
    next_hints=["job_status", "report_finding"],
)
def job_status(job_id: str) -> Dict[str, Any]:
    """Poll any background job by its ``job_id``.

    Discovers the tool that launched the job (from the in-process registry
    or the disk sidecar) and delegates to :func:`poll_job` with the correct
    ``tool_name``, so the tool-specific verdict parser runs.

    Args:
        job_id: The ``job_id`` returned by any ``run_*`` launch tool
            (``run_nmap``, ``run_ffuf``, ``run_hydra``, ``run_sqlmap``,
            ``run_masscan``, ``run_hashcat``, ``run_john``, ``subdomain_enum``,
            etc.).
    """
    # Step 1: check the shared in-process _JOBS dict.
    with _JOBS_LOCK:
        entry = _JOBS.get(job_id)

    if entry is not None:
        tool_name = entry.get("tool", "")
        return poll_job(job_id, tool_name=tool_name)

    # Step 2: not in shared _JOBS — could be sqlmap (private _JOBS) or a
    # reconstructed job.  Discover the tool from the disk sidecar.
    tool_name = _find_job_tool(job_id)

    if tool_name == "sqlmap":
        # sqlmap has its own _JOBS dict and its own poll logic (it doesn't
        # use the shared launch_job helper).  Delegate to sqlmap_status
        # directly — it handles its own _reconstruct_job + _parse_verdict.
        return _poll_sqlmap(job_id)

    if tool_name == "amass":
        # amass has extra per-job metadata (domain, scope, retrieval cache)
        # stored in its own _AMASS_JOBS dict.  Delegate to amass_status so
        # the post-completion name retrieval + scope filtering runs.
        return _poll_amass(job_id)

    if tool_name:
        # Found the tool — poll with it.  poll_job will reconstruct the
        # entry from the disk sidecar and apply the registered verdict
        # parser (if any) as a fallback for the lost closure.
        return poll_job(job_id, tool_name=tool_name)

    # Step 3: no sidecar found.  Try every registered tool name as a
    # last resort — the job may predate the sidecar-writing code, or the
    # log dir may be non-default.
    for try_tool in ("nmap", "ffuf", "hydra", "masscan", "hashcat", "john"):
        result = poll_job(job_id, tool_name=try_tool)
        if result.get("status") != "unknown":
            return result

    return {
        "job_id": job_id,
        "status": "unknown",
        "error": (
            f"no background job with id {job_id!r} found in the in-process "
            f"registry or on disk (it may have been evicted, or the log "
            f"dir is non-default). Try the tool-specific *_status function "
            f"if you know which tool launched it."
        ),
    }


def _poll_sqlmap(job_id: str) -> Dict[str, Any]:
    """Delegate to sqlmap_status (lazy import to avoid circular deps)."""
    try:
        from payloads.sqlmap import sqlmap_status
        return sqlmap_status(job_id)
    except ImportError:
        return {
            "job_id": job_id,
            "status": "unknown",
            "error": "sqlmap module not importable",
        }


def _poll_amass(job_id: str) -> Dict[str, Any]:
    """Delegate to amass_status (lazy import to avoid circular deps)."""
    try:
        from auxiliaries.amass import amass_status
        return amass_status(job_id)
    except ImportError:
        return {
            "job_id": job_id,
            "status": "unknown",
            "error": "amass module not importable",
        }