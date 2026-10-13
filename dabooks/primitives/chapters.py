"""chapters: sweep envelope → chapter-skeleton .ipynb generator (Phase 4).

Writes a NEW notebook file — NEVER attempts in-place cell insertion (a
running kernel cannot edit its own document; the UI owns the file).

## Contract (primitives.txt §4 Phase 4)
- leading cell: the COMMITTED sweep output (heavy-ink rule: only envelopes
  the operator marked for commit ride into the file; default = synthetic or
  an explicitly passed envelope).
- one md+code section per service with evidence placeholders.
- kernelspec: daharness (framework tools), outputs empty, nbformat 4.5.
- The file is written wherever the operator points; the lane never git-
  commits anything itself.

Default output: dabooks/chapters/chapter-<target>-<ts>.ipynb (chapters/ is
gitignored — chapters are working documents, committed when YOU decide).
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

_SAFE_TS_FMT = "%Y%m%d-%H%M%S"


def _slug(target: Optional[str]) -> str:
    """Filesystem-safe target slug. Pure."""
    if not target:
        return "unknown-target"
    slug = "".join(c if c.isalnum() or c in ".-" else "_" for c in target)
    return slug.strip("._") or "unknown-target"


def chapter_dir() -> Path:
    """Output root (working documents; override via COCKPIT_CHAPTER_DIR)."""
    import os
    override = os.getenv("COCKPIT_CHAPTER_DIR")
    if override:
        p = Path(override)
    else:
        p = Path(__file__).resolve().parents[2] / "dabooks" / "chapters"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _md(text: str) -> Dict[str, Any]:
    return {"cell_type": "markdown", "metadata": {}, "source": text}


def _code(text: str) -> Dict[str, Any]:
    return {"cell_type": "code", "execution_count": None,
            "metadata": {}, "outputs": [], "source": text}


def _skeleton(envelope: Any, plan: Dict[str, Any],
              evidence_note: str = "") -> Dict[str, Any]:
    """Build the notebook dict. Services drive sections; the envelope cell
    embeds the EXACT envelope the operator handed in (commit = their act)."""
    from dabooks.primitives.cockpit import VIEW_SERVICES

    cells: List[Dict[str, Any]] = []
    target = plan.get("target") or "unknown"
    shape = plan.get("shape") or "unknown"

    cells.append(_md(
        f"# Chapter — {target}\n\n"
        f"Sweep shape: `{shape}` · generated {_dt.datetime.now().isoformat(timespec='seconds')}\n\n"
        + (f"*{evidence_note}*\n" if evidence_note else "")
    ))

    cells.append(_md("## Sweep envelope (committed)\n\n"
                     "```json\n" + json.dumps(envelope, indent=2, default=str)[:6000] + "\n```\n"))
    cells.append(_code(
        "# Re-derive the cockpit plan from the committed envelope (fresh kernel safe)\n"
        "import dabooks.primitives.cockpit as cockpit\n"
        "PLAN = cockpit.plan(envelope)\n"
        "print(PLAN['target'], PLAN['shape'], len(PLAN['views']), 'views /',\n"
        "      len(PLAN['cards']), 'cards')\n"))

    cells.append(_code(
        "# Lane modules (framework tools are already bare names in this kernel)\n"
        "import importlib\n"
        "import dabooks.primitives.sessions as sess\n"
        "import dabooks.primitives.views as views\n"
        "import dabooks.primitives.proxy as proxy\n"
        "for m in (sess, views, proxy):\n"
        "    importlib.reload(m)\n"))

    for svc in plan.get("views", []):
        scheme = "https" if svc["service"] in ("https",) or svc["port"] in (443, 8443) else "http"
        port = svc["port"]
        cells.append(_md(
            f"## {scheme} · port {port} ({svc.get('source')})\n\n"
            f"Browser lane target: `{scheme}://{target}:{port}` — spawn + screenshot\n"
            f"the front page, then pick VNC (mechanical) or proxied (interactive).\n"))
        cells.append(_code(
            f"# {scheme}://{target}:{port} — session + first screenshot\n"
            f"import asyncio\n"
            f"from dabooks.primitives.sessions import LogPolicy\n"
            f"sn = await SESS.spawn(\"{target}\", {port}, policy=LogPolicy())\n"
            f"sn = await SESS.wait_ready(\"{target}\", {port}, timeout_s=30)\n"
            f"print(sn.status, sn.error or '')\n"
            f"png = await SESS.screenshot(\"{target}\", {port})\n"
            f"print(f'{{len(png)}} bytes')\n"
            f"# then: views.VNCView(SESS, \"{target}\", {port}).refresh_sync()\n"
            f"#  or:  views.ProxiedView(await proxy.ensure_proxy(), \"{scheme}\", \"{target}\", {port}).frame()\n"))
        cells.append(_code(
            f"# evidence placeholder — paste probe output under this heading\n"
            f"# (curl headers, ffuf hits, ZAP alerts, creds found)\n"
            f"EVIDENCE_{port} = []\n"))

    for svc in plan.get("cards", []):
        port = svc["port"]
        cells.append(_md(
            f"## {svc['service']} · port {port} ({svc.get('source')})\n\n"
            "Banner status card — connect + first bytes, bounded, fail-closed.\n"))
        cells.append(_code(
            f"# {svc['service']} banner on {target}:{port}\n"
            f"import asyncio\n"
            f"_r, _w = await asyncio.wait_for(\n"
            f"    asyncio.open_connection(\"{target}\", {port}), timeout=5)\n"
            f"try:\n"
            f"    banner = await asyncio.wait_for(_r.read(512), timeout=5)\n"
            f"finally:\n"
            f"    _w.close()\n"
            f"print(banner.decode('utf-8', 'replace').strip())\n"))
        cells.append(_code(
            f"# evidence placeholder\n"
            f"EVIDENCE_{port} = []\n"))

    if not plan.get("views") and not plan.get("cards"):
        cells.append(_md("*No view/card services parsed from this envelope.*\n"))

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "daharness (framework tools)",
                           "language": "python", "name": "daharness"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write_chapter(envelope: Any, plan: Dict[str, Any],
                  filename: Optional[str] = None,
                  evidence_note: str = "") -> Path:
    """Write the chapter notebook; returns the path. NEVER touches git.
    Default name: chapter-<slug>-<ts>.ipynb in chapter_dir()."""
    nb = _skeleton(envelope, plan, evidence_note)
    ts = _dt.datetime.now().strftime(_SAFE_TS_FMT)
    name = filename or f"chapter-{_slug(plan.get('target'))}-{ts}.ipynb"
    if not name.endswith(".ipynb"):
        name += ".ipynb"
    out = chapter_dir() / name
    out.write_text(json.dumps(nb, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    return out