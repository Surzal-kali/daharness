#!/usr/bin/env bash
# code-server boot-time framework runtime provisioning (idempotent — mounted
# into the lsio image's /custom-cont-init.d hook, runs at 10-* after
# 00-extensions-and-continue.sh on every boot).
#
# The image (dockered/code-server.Dockerfile) bakes the runtime lane the
# jupyter lane carries (jupyter.Dockerfile): /opt/framework-venv (pinned
# framework deps), build-time stubs in /opt/framework-stubs, the daharness
# kernelspec in /usr/local/share/jupyter/kernels, and /usr/local/bin symlinks
# for python/pip/jupyter. This hook does the two things an image cannot:
#
#   1. Regenerate stubs from the LIVE bind-mounted tree (fresh tool code every
#      container start; new tools don't wait for a rebuild) — run as the
#      service user so generator writes into the shared tree (the generated
#      builtins block in dabooks/ruff.toml) land as PUID (kali), not root.
#      Failure is non-fatal: the baked stubs keep the editor lane working,
#      just potentially stale — same degradation contract as 00-*.
#
#   2. Hand the venv + stubs to the service user (PUID/PGID are runtime env,
#      the image bakes root) and provision the container-side editor settings
#      (interpreter + stubPath) into the persistent /config volume — key-wise
#      only-if-absent, operator edits win and survive every reboot.
#
# Why the settings live in /config/data/User/settings.json and NOT in a
# pyrightconfig.json inside the workspace: /config/workspace IS the host tree
# — a config file in it would leak onto the host's own pyright lanes (nvim,
# host VS Code). Workspace .vscode/settings.json still wins for the keys it
# declares; those keys (extraPaths, diagnostics) are lane-neutral already.
# The workspace's python.defaultInterpreterPath points at the host's Python
# 3.14 venv, which cannot run inside this container — operator picks the
# /opt/framework-venv interpreter once in the UI and workspaceStorage (the
# /config volume) remembers it.

set -u

LOG_TAG="[code-server-runtime]"
VENV=/opt/framework-venv
STUBS=/opt/framework-stubs
OWNERSHIP="${PUID:-1000}:${PGID:-1000}"

echo "$LOG_TAG framework runtime lane (venv: $VENV, stubs: $STUBS)"

if [ ! -x "$VENV/bin/python" ]; then
  echo "$LOG_TAG WARN: baked venv missing (image without the runtime lane); skipping"
else
  # Ownership once per venv: the operator may pip-install into the lane from
  # the editor terminal, so abc must own it. Sentinel-guarded — chown -R over
  # the whole venv every boot is wasted seconds after the first pass.
  if [ ! -e "$VENV/.runtime-owned" ]; then
    chown -R "$OWNERSHIP" "$VENV" 2>/dev/null || true
    : >"$VENV/.runtime-owned" 2>/dev/null || true
    chown "$OWNERSHIP" "$VENV/.runtime-owned" 2>/dev/null || true
    echo "$LOG_TAG venv handed to ${OWNERSHIP} (first boot)"
  fi

  # Stubs regen runs AS THE SERVICE USER (su): writes into the shared tree
  # (dabooks/ruff.toml builtins block) must carry host-lane ownership, and
  # the stub files the extension reads land service-user-owned. Resolve the
  # name by PUID (the image bakes abc at uid 911; lsio's runtime init
  # remaps it to PUID before cont-init.d runs) — if nothing owns that uid
  # yet, keep the baked stubs rather than generate under the wrong identity.
  if [ -f /config/workspace/scripts/gen_repl_stubs.py ] && [ -f /config/workspace/tool_repl.py ]; then
    chown -R "$OWNERSHIP" "$STUBS" 2>/dev/null || true
    SU_USER="$(getent passwd "${PUID:-1000}" 2>/dev/null | cut -d: -f1)"
    if [ -n "${SU_USER}" ]; then
      echo "$LOG_TAG regenerating stubs from the live tree (as ${SU_USER})"
      su -s /bin/bash "${SU_USER}" -c \
        "cd /config/workspace && FRAMEWORK_STUBS_DIR=${STUBS} PYTHONDONTWRITEBYTECODE=1 '${VENV}/bin/python' scripts/gen_repl_stubs.py"
      echo "$LOG_TAG live-tree stub regeneration exit: $?"
    else
      echo "$LOG_TAG WARN: no user owns PUID=${PUID:-1000} (lsio abc remap not applied?); keeping baked stubs"
    fi
  else
    echo "$LOG_TAG WARN: framework bind mount incomplete; keeping baked stubs"
  fi
fi

# Container-side editor settings — key-wise only-if-absent merge into the
# code-server user settings (operator-owned file, never clobbered). A JSONC
# (commented) settings file is left untouched entirely — never rewrite what
# we cannot parse.
if [ -x "$VENV/bin/python" ]; then
  mkdir -p /config/data/User
  FRAMEWORK_VENV="$VENV" FRAMEWORK_STUBS_DIR="$STUBS" \
    "$VENV/bin/python" - <<'PY'
import json, os
from pathlib import Path

path = Path("/config/data/User/settings.json")
keys = {
    "python.defaultInterpreterPath": f"{os.environ['FRAMEWORK_VENV']}/bin/python",
    "python.venvPath": "/opt",
    "python.analysis.stubPath": os.environ["FRAMEWORK_STUBS_DIR"],
    "basedpyright.analysis.stubPath": os.environ["FRAMEWORK_STUBS_DIR"],
    # Test discovery lane: ms-python's Testing view (pytest). Interpreter is
    # already pinned above and MUST stay consistent — discovery shells out to
    # `<interpreter> -m pytest` (pytest >=7 discovery), and a stale default
    # pointing at a pytest-less venv kills the whole pane. testpaths +
    # python_files live in pyproject [tool.pytest.ini_options]; args here stay
    # minimal so pytest owns discovery, not the editor.
    "python.testing.pytestEnabled": True,
    "python.testing.unittestEnabled": False,
    "python.testing.autoTestDiscoverOnSaveEnabled": True,
}
current = {}
if path.exists():
    try:
        current = json.loads(path.read_text())
    except Exception:
        print("[code-server-runtime] settings.json unreadable (JSONC or corrupt) — skipping")
        raise SystemExit(0)
missing = {k: v for k, v in keys.items() if k not in current}
if missing:
    current.update(missing)
    path.write_text(json.dumps(current, indent=4) + "\n")
    print(f"[code-server-runtime] provisioned editor settings: {', '.join(missing)}")
else:
    print("[code-server-runtime] editor settings already present; skipping")
PY
  chown "${OWNERSHIP}" /config/data/User/settings.json /config/data/User 2>/dev/null || true
fi

# Sanity: the kernelspec argv points at the live tree by absolute path — warn
# (do not rewrite) if the mount drifts from /config/workspace.
if [ ! -f /config/workspace/tool_repl.py ] && [ -f /usr/local/share/jupyter/kernels/daharness/kernel.json ]; then
  echo "$LOG_TAG WARN: daharness kernelspec argv points at /config/workspace/tool_repl.py, which is absent (bind mount drift?) — notebooks will need the operator's workspace path"
fi

# Executed directly by s6 (not sourced) — but the tail keeps the same
# never-trap-the-hook contract as the jupyter before-notebook.d hooks.
return 0 2>/dev/null || exit 0