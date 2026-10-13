"""selftest: Phase-0 smoke test for the kernel lane.

Runs identically in three contexts (raw python, notebook cell, pytest):

    # raw venv:
    python -m dabooks.primitives.selftest
    # notebook cell:
    from dabooks.primitives import selftest
    res = await selftest.run(); selftest.render(res)
    # pytest:
    pytest tests/test_primitives_phase0.py -q

Verifies the Phase-0 ship WITHOUT playwright (that arrives Phase 1):
1. injected-helper evidence: presence + sync/async shape REPORTED (the
   live kernel probed sync across pluck/keys/find/grep/scratch).
2. fail-closed first() never raises (None / empty / non-iterable).
3. sync pluck/keys/find/grep walk the deep demo envelope with the
   ground-truth semantics (list-values-whole, dicts-dropped, sorted keys).
4. summarize_parsed produces one stable line per service.
5. wait_for fail-closed on timeout (the ONLY async member of Phase 0).
"""

from __future__ import annotations

import inspect
import sys
from typing import Any, Dict as TDict, List

from dabooks.primitives.kernelgate import (
    _injected, first, find, grep, keys, pluck, to_json, wait_for,
)
from dabooks.primitives.sweeps import demo_envelope, summarize_parsed


async def run() -> TDict[str, Any]:
    """Execute the Phase-0 checks. Every check is an explicit bool;
    unexpected exceptions are caught and reported as check failures
    (fail-closed), never escaped to wedge the lane."""
    checks: TDict[str, bool] = {}
    detail: List[str] = []

    def _safe(label: str, fn, *args) -> None:
        try:
            checks[label] = bool(fn(*args))
        except Exception as exc:  # noqa: BLE001 — fail-closed on purpose
            checks[label] = False
            detail.append(f"{label} raised: {exc!r}")

    # 1. injected-helper evidence
    try:
        pl, ks = _injected("pluck"), _injected("keys")
        checks["helper_detect"] = True
        if pl is None and ks is None:
            detail.append("no injected pluck/keys — pure local-mirror walk "
                          "(plain-python context, or stubbed kernel)")
        else:
            probe = pl if pl is not None else ks
            shape = "async" if inspect.iscoroutinefunction(probe) else "sync"
            detail.append(f"injected helpers: pluck={pl is not None} "
                          f"keys={ks is not None} shape={shape}")
    except Exception as exc:  # noqa: BLE001
        checks["helper_detect"] = False
        detail.append(f"helper_detect raised: {exc!r}")

    # 2. fail-closed first() — every edge case independently
    checks["first_none"] = first(None) is None
    checks["first_empty"] = first([], "empty list") is None
    checks["first_noniter"] = first(42, "scalar") is None
    checks["first_hit"] = first([1, 2], "two") == 1

    # 3. sync ground-truth parse quartet on the deep synthetic envelope
    env = demo_envelope()
    _safe("pluck_routes", lambda: pluck(env, "routes") == [["/", "/admin/", "/static/app.js"]])
    _safe("pluck_ports", lambda: sorted(pluck(env, "port")) == [22, 80])
    _safe("keys_sorted", lambda: keys(env) == sorted(keys(env)))
    _safe("keys_present", lambda: all(k in keys(env) for k in ("port", "routes", "sweep_id")))
    _safe("find_banner", lambda: len(find(env, "banner")) == 2)
    _safe("grep_hit", lambda: len(grep(env, r"OpenSSH|nginx")) >= 2)

    # 4. summarize stable
    _safe("summarize", lambda: summarize_parsed(env) == ["port 80 → http", "port 22 → ssh"])

    # 5. wait_for fail-closed (timeout returns None + logs, never raises)
    async def _sleepy() -> None:
        import asyncio
        await asyncio.sleep(30)
    try:
        checks["wait_for_timeout"] = await wait_for(_sleepy(), 0.05, "selftest-timeout") is None
    except Exception as exc:  # noqa: BLE001
        checks["wait_for_timeout"] = False
        detail.append(f"wait_for_timeout raised: {exc!r}")

    # 6. to_json deterministic
    checks["to_json"] = to_json({"a": 1}) == '{"a": 1}'

    all_ok = all(checks.values())
    return {"status": "pass" if all_ok else "fail",
            "checks": checks, "detail": detail}


def render(res: TDict[str, Any]) -> str:
    """Pretty one-glance result card for notebook output."""
    icon = {True: "✓", False: "✗"}
    body = "\n".join(f"  {icon[v]} {k}" for k, v in res["checks"].items())
    return f"phase-0 selftest: {res['status']}\n{body}\n" + "\n".join(res["detail"])


if __name__ == "__main__":
    import asyncio
    result = asyncio.run(run())
    print(render(result))
    sys.exit(0 if result["status"] == "pass" else 1)