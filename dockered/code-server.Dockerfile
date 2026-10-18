# =============================================================================
# code-server + framework runtime lane
#
# The stock lscr.io/linuxserver/code-server image is an editor with no runtime
# tooling: no Python, no framework dependencies, no REPL stubs, no daharness
# kernel — over the workspace bind mount it is just a fancy tree viewer. This
# image gives the editor lane the same runtime the jupyter lane carries
# (dockered/jupyter.Dockerfile), with the same two-layer stub discipline:
#
#   BUILD (below): copy the tree in (bind mounts don't exist at build time),
#   generate into /opt/framework-stubs, bake the daharness kernelspec. The
#   image is never stale.
#
#   BOOT (dockered/code-server-init/10-framework-runtime.sh, mounted into
#   custom-cont-init.d): regenerate stubs from the LIVE bind mount
#   /config/workspace (fresh tool code every container start; new tools don't
#   wait for a rebuild), hand the venv and stubs to the service user, and
#   provision the container-side editor settings (interpreter + stubPath)
#   into the persistent /config volume only-if-absent.
#
# Marketplace discipline (revised 2026-10-18): extensions come through the
# Microsoft gallery — OpenVSX ms-python builds strip the bundled
# python-env-tools "pet" binary, which kills interpreter discovery
# (`spawn .../python-env-tools/bin/pet ENOENT`). Still NO Copilot VSIX:
# the assistant stays continue.continue → host-lane Ollama (local-network
# only). See code-server-init/00-extensions-and-continue.sh for the full
# rationale and the pre-gallery-swap repair path.
# =============================================================================
FROM lscr.io/linuxserver/code-server:latest

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip git \
    && rm -rf /var/lib/apt/lists/*

# ── Framework runtime venv ────────────────────────────────────────────────────
# Same pinned dependency set as the jupyter image (plus jupyter_console, so
# tool_repl's printed "jupyter console --existing" hint actually works in this
# lane). Ubuntu's PEP-668 externally-managed environment is sidestepped with a
# private venv instead of --break-system-packages.
COPY requirements.txt /tmp/framework-requirements.txt
RUN python3 -m venv /opt/framework-venv \
    && /opt/framework-venv/bin/pip install --no-cache-dir --upgrade pip wheel \
    && /opt/framework-venv/bin/pip install --no-cache-dir -r /tmp/framework-requirements.txt \
    && /opt/framework-venv/bin/pip install --no-cache-dir \
        chromadb==1.5.9 \
        pydantic-ai==2.35.0 \
        ipywidgets papermill jupyter_console \
    && /opt/framework-venv/bin/pip install --no-cache-dir --no-deps --force-reinstall \
        mcp==1.29.0 mcp-types==2.0.0 \
    && rm -f /tmp/framework-requirements.txt

# Editor shell lane: `python` / `pip` / `jupyter` resolve to the framework
# venv in every shell (code-server terminals, exec, boot hooks). Wrapper, not
# symlink, for python: CPython finds pyvenv.cfg relative to the REAL
# executable path, so a /usr/local/bin/python symlink to the venv python
# resolves as system python (prefix /usr, no venv site-packages). Console
# scripts are different — pip and jupyter carry shebangs straight at the venv
# interpreter, so plain symlinks are honest for them.
RUN printf '#!/bin/sh\nexec /opt/framework-venv/bin/python "$@"\n' >/usr/local/bin/python \
    && printf '#!/bin/sh\nexec /opt/framework-venv/bin/python "$@"\n' >/usr/local/bin/python3 \
    && chmod +x /usr/local/bin/python /usr/local/bin/python3 \
    && ln -sf /opt/framework-venv/bin/pip /usr/local/bin/pip \
    && ln -sf /opt/framework-venv/bin/pip /usr/local/bin/pip3 \
    && ln -sf /opt/framework-venv/bin/jupyter /usr/local/bin/jupyter

# ── Build-time stub generation ────────────────────────────────────────────────
# Framework copy for stub generation (bind mounts don't exist in the build
# container). Same pruned set as jupyter.Dockerfile — keep the three files in
# sync (jupyter.Dockerfile, this file, scripts/gen_repl_stubs.py imports).
COPY daharness/ /tmp/framework-build/daharness/
COPY utils/ /tmp/framework-build/utils/
COPY auxiliaries/ /tmp/framework-build/auxiliaries/
COPY payloads/ /tmp/framework-build/payloads/
COPY listeners/ /tmp/framework-build/listeners/
COPY encoders/ /tmp/framework-build/encoders/
COPY constants.py tool_repl.py scripts/gen_repl_stubs.py /tmp/framework-build/tmp-copy/
RUN mv /tmp/framework-build/tmp-copy/gen_repl_stubs.py /tmp/framework-build/gen_repl_stubs.py \
    && mkdir -p /tmp/framework-build/scripts \
    && mv /tmp/framework-build/gen_repl_stubs.py /tmp/framework-build/scripts/gen_repl_stubs.py \
    && mv /tmp/framework-build/tmp-copy/constants.py /tmp/framework-build/tmp-copy/tool_repl.py /tmp/framework-build/

# Same HOME dance as jupyter.Dockerfile: stub generation dynamic-imports the
# tool tree and scapy's XDG probe would otherwise bake a root-owned
# ~/.config into the image.
RUN mkdir -p /opt/framework-stubs \
    && cd /tmp/framework-build \
    && HOME=/tmp/build-home FRAMEWORK_STUBS_DIR=/opt/framework-stubs \
       PYTHONDONTWRITEBYTECODE=1 /opt/framework-venv/bin/python scripts/gen_repl_stubs.py \
    && rm -rf /tmp/framework-build /tmp/build-home

# ── daharness kernelspec (system kernel dir — visible to every frontend) ──────
# ms-toolsai.jupyter's kernel picker and `jupyter console --kernel=daharness`
# both find it. argv points at the baked venv + the live tree's tool_repl.py
# (the bind mount is /config/workspace — compose's DEFAULT_WORKSPACE); same
# shape as the jupyter image's spec, which points at /opt/conda/bin/python.
RUN mkdir -p /usr/local/share/jupyter/kernels/daharness \
    && cat > /usr/local/share/jupyter/kernels/daharness/kernel.json <<'EOF'
{
  "argv": [
    "/opt/framework-venv/bin/python",
    "/config/workspace/tool_repl.py",
    "--kernel",
    "-f",
    "{connection_file}"
  ],
  "display_name": "daharness (framework tools)",
  "language": "python",
  "interrupt_mode": "message",
  "metadata": {
    "debugger": true
  }
}
EOF