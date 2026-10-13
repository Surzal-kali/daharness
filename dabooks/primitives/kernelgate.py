"""kernelgate: defensive access to the kernel-injected helper names.

PHASE-0 PRIMITIVE — kernel lane only. Not a framework tool. NO playwright.

## Contract (from primitives.txt §1 / §5)
- "Work envelopes are deep nested JSON: pluck/find return FLAT LISTS at any
  depth — every scalar param needs [0]. Parse defensively everywhere."
- "Human-waits FAIL CLOSED on timeout, with a log line."
- Stateless → safe for fresh-kernel re-derivation.

## Sync by EVIDENCE (live kernel, 2026-10-13, cockpit.ipynb cell 2)
The injected helpers probed sync in this kernel: **pluck, keys, find, grep,
scratch — all sync**. The await rule verified to bind framework *tools*
only (async wrappers from ``_make_tool_wrapper``). Everything here is sync
so it composes in f-strings, list comps, and pytest.

## The ground-truth semantics (read from tool_repl.py:740-852)
The shim's local walkers mirror the injected helpers EXACTLY, so behavior
is identical with or without delegation:

- ``pluck(env, key)``: every value for key at any depth, list values kept
  WHOLE (one entry per occurrence — no flattening), dicts DROPPED.
- ``keys(env)``: sorted distinct key names.
- ``find(env, key, value=...)``: with value → ``{"key","value","path"}``
  dicts (loose str-coercion equality); without → bare values.
- ``grep(env, rx)``: ``(path, line)`` tuples, multi-line strings split,
  dict keys matched too, sorted by path, case-insensitive by default.

If an injected helper ever returns a coroutine, :func:`pluck`/​:func:`keys`
raise a loud TypeError pointing at the injected name instead of handing
back a half-coroutine (regression-tested in tests/test_primitives_phase0.py).

Shadow hazard (hit live): a notebook cell doing ``from kernelgate import
pluck`` overwrites the INJECTED pluck in user_ns — IPython stores cell-level
imports there — so ``_injected("pluck")`` would find our own shim and call
itself forever. :func:`_injected` therefore never returns our own names;
the local mirror walk answers instead. Lane hygiene still preferred: prefer
``import dabooks.primitives.kernelgate as kg`` style access in cells.

This module is the kernel-lane twin of the CLI-REPL scope lane: a lane-side
shim, never a framework tool. The graduation seam is tested — no
``@framework_tool`` decorator may appear in this package, and importing
this package alone must never drag ``daharness`` into ``sys.modules``.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Dict as TDict, Iterable, List, Optional, Tuple

log = logging.getLogger("dabooks.kernelgate")

_MISSING = object()  # sentinel, mirrors tool_repl.find's default


# --- envelope unwrap (mirrors tool_repl._envelope_payload) --------------
def _envelope_payload(envelope: Any) -> Any:
    """Unwrap ``{"status": ..., "result": <structured>|<bare str + stdout>}``.
    Falls back to envelope["stdout"] (JSON-parsed when possible) when
    "result" is absent — bare-string tool output carries data there. The
    envelope IS the payload when neither is present."""
    if not isinstance(envelope, dict):
        return envelope
    if "status" not in envelope:
        return envelope
    inner = envelope.get("result")
    if inner is not None:
        return inner
    raw = envelope.get("stdout", "")
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def _value_matches(actual: Any, expected: Any) -> bool:
    """Loose equality for find(value=...) — str-coercion fallback."""
    if actual == expected:
        return True
    try:
        return str(actual) == str(expected)
    except Exception:
        return False


# --- local walkers (ground-truth mirror of the injected helpers) --------
def _walk_pluck(obj: Any, key: str, out: List[Any]) -> None:
    """Every value for `key`, list values kept WHOLE, dicts DROPPED.
    Exactly tool_repl.pluck's semantics (find + drop-dict filter)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and not isinstance(v, dict):
                out.append(v)
            _walk_pluck(v, key, out)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk_pluck(item, key, out)


def _walk_keys(obj: Any, seen: set) -> None:
    """Sorted distinct key names — tool_repl.keys's semantics."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            seen.add(k)
            _walk_keys(v, seen)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk_keys(item, seen)


def _walk_find(obj: Any, key: str, value: Any, path: str, out: list) -> None:
    """Deep find — tool_repl.find's semantics: values, or {key,value,path}
    dicts with loose equality when `value` is given."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{path}.{k}"
            if k == key:
                if value is _MISSING:
                    out.append(v)
                elif _value_matches(v, value):
                    out.append({"key": k, "value": v, "path": child})
            _walk_find(v, key, value, child, out)
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _walk_find(item, key, value, f"{path}[{i}]", out)


def _walk_grep(obj: Any, rx, path: str, out: List[Tuple[str, str]]) -> None:
    """Deep grep — tool_repl.grep's semantics: (path, line) tuples,
    multi-line split, dict keys matched too."""
    if isinstance(obj, str):
        for line in obj.splitlines():
            if rx.search(line):
                out.append((path, line.strip()))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if rx.search(str(k)):
                out.append((f"{path}.{k}", f"<key> {k}"))
            _walk_grep(v, rx, f"{path}.{k}", out)
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _walk_grep(item, rx, f"{path}[{i}]", out)


# --- injected-helper probe ---------------------------------------------
# Our own public names. A notebook cell doing ``from kernelgate import pluck``
# WRITES our shim into shell.user_ns, overwriting the injected helper — and
# ``_injected`` would then find our own function and call it recursively
# forever (RecursionError; hit live on 2026-10-13). The guard below makes
# that shadow harmless: shim-in-user_ns counts as "no helper", so the local
# mirror walk answers instead.
_OWN_NAMES = ("pluck", "keys", "find", "grep", "scratch", "first")


def _injected(name: str):
    """Return the kernel-injected `name` callable, or None when absent,
    shadowed by our own shim, or running outside a kernel (plain pytest).
    One shell lookup per call — cheap, and respects late namespace
    mutation."""
    try:
        from IPython import get_ipython
        shell = get_ipython()
        if shell is None:
            return None
        val = shell.user_ns.get(name)
        if not callable(val):
            return None
        if name in _OWN_NAMES and getattr(val, "__module__", "") == __name__:
            return None  # our own shim (possibly a stale pre-reload object)
        return val
    except Exception:
        return None


def _coroutine_guard(result: Any, name: str) -> None:
    """Loud rejection of an async injected helper — see module docstring."""
    if inspect.iscoroutine(result):
        raise TypeError(
            f"kernelgate: injected helper {name!r} returned a coroutine — "
            f"call it directly with await (await {name}(...)) instead of "
            f"the kernelgate shim"
        )


# --- the public surface (mirrors the injected contract 1:1) -------------
def first(seq, what: str = "value") -> Optional[Any]:
    """Fail-closed [0]: seq[0] if seq is a non-empty sequence, else None +
    a log line (never IndexError)."""
    if seq is None:
        log.warning("kernelgate.first: %s missing entirely (None)", what)
        return None
    try:
        items = list(seq)
    except TypeError:
        log.warning("kernelgate.first: %s not iterable (%r)", what, seq)
        return None
    if not items:
        log.warning("kernelgate.first: %s empty — fail-closed None", what)
        return None
    return items[0]


def pluck(env: Any, key: str) -> List[Any]:
    """Flat list of every `key` value at any depth (list values kept whole,
    dicts dropped). Delegates to the injected helper when present; the local
    mirror walk otherwise. Sync by evidence."""
    helper = _injected("pluck")
    if helper is not None:
        try:
            result = helper(env, key)
            _coroutine_guard(result, "pluck")
            return result if isinstance(result, list) else [result]
        except TypeError:
            raise
        except Exception as exc:
            log.warning("injected pluck failed (%s); local-walk fallback", exc)
    out: List[Any] = []
    _walk_pluck(_envelope_payload(env), key, out)
    return out


def keys(env: Any) -> List[str]:
    """Sorted distinct key names at any depth. Delegates to the injected
    helper when present; local mirror otherwise."""
    helper = _injected("keys")
    if helper is not None:
        try:
            result = helper(env)
            _coroutine_guard(result, "keys")
            if isinstance(result, (list, tuple)):
                return list(result)
        except TypeError:
            raise
        except Exception as exc:
            log.warning("injected keys failed (%s); local-walk fallback", exc)
    seen: set = set()
    _walk_keys(_envelope_payload(env), seen)
    return sorted(seen)


def find(env: Any, key: str, value: Any = _MISSING):
    """Deep find with tool_repl's exact semantics (values, or
    {key,value,path} dicts under loose equality when `value` is given)."""
    helper = _injected("find")
    if helper is not None:
        try:
            if value is _MISSING:
                result = helper(env, key)
            else:
                result = helper(env, key, value=value)
            _coroutine_guard(result, "find")
            if isinstance(result, list):
                return result
        except TypeError:
            raise
        except Exception as exc:
            log.warning("injected find failed (%s); local-walk fallback", exc)
    out: list = []
    payload = _envelope_payload(env)
    root = "result" if payload is not env else "$"
    _walk_find(payload, key, value, root, out)
    return out


def grep(env: Any, rx: str, ignore_case: bool = True) -> List[Tuple[str, str]]:
    """(path, line) tuples per tool_repl.grep: multi-line strings split,
    dict keys matched, sorted by path, case-insensitive by default."""
    helper = _injected("grep")
    if helper is not None:
        try:
            result = helper(env, rx, ignore_case=ignore_case)
            _coroutine_guard(result, "grep")
            if isinstance(result, list):
                return [tuple(x) for x in result]
        except TypeError:
            raise
        except Exception as exc:
            log.warning("injected grep failed (%s); local-walk fallback", exc)
    import re
    flags = re.IGNORECASE if ignore_case else 0
    pattern = re.compile(rx, flags)
    out: List[Tuple[str, str]] = []
    payload = _envelope_payload(env)
    root = "result" if payload is not env else "$"
    _walk_grep(payload, pattern, root, out)
    return sorted(out, key=lambda pair: pair[0])


# --- fail-closed wait (the "human-waits fail closed" pattern) ----------
async def wait_for(coro, timeout_s: float, what: str = "operation") -> Any:
    """Await `coro` bounded by `timeout_s`; timeout ⇒ log + return None
    (fail-closed). The one async member of Phase 0 — genuine loop work."""
    import asyncio
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        log.warning("kernelgate.wait_for: %s timed out after %.1fs — fail-closed",
                    what, timeout_s)
        return None


def to_json(obj: Any) -> str:
    """Compact JSON dump for logging/display of envelopes."""
    return json.dumps(obj, default=str, ensure_ascii=False)