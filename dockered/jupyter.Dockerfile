# =============================================================================
# JupyterLab + daharness kernel
#
# This image is separate from open-terminal because the latter's entrypoint
# starts the framework gateway and Open Terminal server. The notebook image
# only needs the framework Python dependencies and the checked-out tree,
# which is bind-mounted by docker-compose at runtime.
# =============================================================================
FROM quay.io/jupyter/base-notebook:latest

USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/framework-requirements.txt

RUN python -m pip install --no-cache-dir -r /tmp/framework-requirements.txt \
    && python -m pip install --no-cache-dir \
        chromadb==1.5.9 \
        pydantic-ai==2.35.0 \
        ipywidgets papermill \
    && python -m pip install --no-cache-dir --no-deps --force-reinstall \
        mcp==1.29.0 mcp-types==2.0.0 \
    && rm -f /tmp/framework-requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# PyPI extension-manager patch (2026-10). PyPI removed the deprecated XML-RPC
# `browse` method after abuse-triggered outages; JupyterLab 4.6.x lists prebuilt
# extensions through it, so the Extensions panel 500s with a masked
# UnboundLocalError. Upstream rewrite is tracked at jupyterlab/jupyterlab#19932.
# The patch re-raises non-throttling XMLRPC faults (upstream main semantics) and
# degrades the dead browse call gracefully: the panel loads and install-by-name
# works; Discover search returns only language packs until upstream ships a
# replacement data source. No-op if the base image later carries fixed code.
# ─────────────────────────────────────────────────────────────────────────────
COPY dockered/patch_jupyterlab_pypi.py /tmp/patch_jupyterlab_pypi.py
RUN python /tmp/patch_jupyterlab_pypi.py \
    && rm -f /tmp/patch_jupyterlab_pypi.py

# ─────────────────────────────────────────────────────────────────────────────
# notebook-intelligence model-list patch (2026-10). Two defects in the 6.2.0
# wheel break every AI lane in this container: the Copilot chat fallback list
# carries premium ids that 400 with model_not_supported on the lab's
# free_educational_quota SKU (which also marks every /models entry
# model_picker_enabled=false, so the dynamic catalogue is always empty), and
# the native Ollama inline-completion provider hardcodes untagged model ids
# that only resolve to :latest tags this Ollama host doesn't have. The patch
# swaps in the verified working Copilot ids and the host's actual installed
# model tags. See patch_nbi_providers.py for details; exact-text hunks, so a
# notebook-intelligence upgrade just makes the hunks skip.
# ─────────────────────────────────────────────────────────────────────────────
COPY dockered/patch_nbi_providers.py /tmp/patch_nbi_providers.py
RUN python /tmp/patch_nbi_providers.py \
    && rm -f /tmp/patch_nbi_providers.py

RUN mkdir -p /usr/local/share/jupyter/kernels/daharness \
    && cat > /usr/local/share/jupyter/kernels/daharness/kernel.json <<'EOF'
{
  "argv": [
    "/opt/conda/bin/python",
    "/home/jovyan/framework/tool_repl.py",
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
# ─────────────────────────────────────────────────────────────────────────────
# NBI env-level config seed. Jupyter's Settings UI persists only to the user
# config (~/.jupyter/nbi/config.json) and drops it on every image recreate;
# NBI's load() layers the env prefix file under it, so a baked config.json in
# /opt/conda/share/jupyter/nbi/ is a rebuild-proof admin baseline (user
# changes layer on top at runtime). Keep in sync with the file baked by the
# inline heredoc:
#   chatbook_backend_kernel=daharness — Chatbook code cells execute in the
#   framework kernel, so generated code sees the preloaded awaitable
#   @framework_tool stubs. resolve_backend_kernel() hard-fails on a
#   not-installed name, and the kernelspec is baked above.
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /opt/conda/share/jupyter/nbi \
    && cat > /opt/conda/share/jupyter/nbi/config.json <<'EOF'
{
  "chatbook_backend_kernel": "daharness"
}
EOF

# ─────────────────────────────────────────────────────────────────────────────
# Editor lane inside JupyterLab: jupyterlab-lsp → pyright (bundled), reading
# the same tool stubs the VS Code lane reads. The pip pyright wheel bundles
# the full npm pyright package (incl. langserver.index.js); node runs it
# directly, no npm install. config.d registers it as a custom jupyter-lsp
# spec under the `pyright` key — config wins over autodetect for that key.
# (This jupyter-lsp 2.3.1 build CRASHES on the documented `"pylsp": []` opt-out
# — 'list' object has no attribute 'get' in _collect_language_servers — so pylsp
# coexists. Its jedi-based squiggles will also report the runtime-injected tool
# names; the frontend lets LSP diagnostics be disabled per server, or uninstall
# python-lsp-server from the image if its noise bothers the lane.)
#
# Stubs come from the framework's own scripts/gen_repl_stubs.py (same
# generator, same no-ChromaDB discovery the kernel uses — zero drift from what
# actually executes). Two-layer system, both required:
#
#   BUILD (below): copy the tree in (bind mounts don't exist at build time),
#   generate into /opt/conda/share/framework-stubs. Image is never stale.
#
#   BOOT (10-refresh-stubs.sh in before-notebook.d): regenerate from the RUNTIME
#   bind mount /home/jovyan/framework (fresh tool code every container start;
#   new tools don't wait for a rebuild), overwriting the baked copies. Then copy
#   the baked pyrightconfig.json into /home/jovyan/work — the notebooks dir is a
#   host bind-mount, so anything the image bakes inside it is shadowed.
#
# Config: pyright reads its settings from pyrightconfig.json at the workspace
# root; verified live that stubPath there resolves every tool alias cleanly
# (scripts/pyright_stub_probe.py + scripts/lsp_session_e2e.py prove it both
# raw-stdio and through jupyter-lsp's own session machinery). The
# jupyterlab-lsp settings-delivery path (workspace/didChangeConfiguration) is
# unreliable for this (pyright restarts its analysis service and only honors a
# subset), so the config file wins.
# ─────────────────────────────────────────────────────────────────────────────
COPY dockered/jupyter-lsp-pyright.json /opt/conda/etc/jupyter/jupyter_server_config.d/jupyter-lsp-pyright.json
COPY dockered/10-refresh-stubs.sh /usr/local/bin/before-notebook.d/10-refresh-stubs.sh
RUN chmod +x /usr/local/bin/before-notebook.d/10-refresh-stubs.sh

# Framework copy for build-time stub generation (bind mounts don't exist in the
# build container). Pruned to what the generator + its imports need — keep in
# sync with scripts/gen_repl_stubs.py's imports (tool_repl, daharness, utils,
# auxiliaries, payloads, listeners, constants, requirements deps already above).
COPY daharness/ /home/jovyan/framework-build/daharness/
COPY utils/ /home/jovyan/framework-build/utils/
COPY auxiliaries/ /home/jovyan/framework-build/auxiliaries/
COPY payloads/ /home/jovyan/framework-build/payloads/
COPY listeners/ /home/jovyan/framework-build/listeners/
COPY encoders/ /home/jovyan/framework-build/encoders/
COPY constants.py tool_repl.py scripts/gen_repl_stubs.py /home/jovyan/framework-build/tmp-copy/
RUN mv /home/jovyan/framework-build/tmp-copy/gen_repl_stubs.py /home/jovyan/framework-build/scripts_gen_repl_stubs.py \
    && mkdir -p /home/jovyan/framework-build/scripts \
    && mv /home/jovyan/framework-build/scripts_gen_repl_stubs.py /home/jovyan/framework-build/scripts/gen_repl_stubs.py \
    && mv /home/jovyan/framework-build/tmp-copy/constants.py /home/jovyan/framework-build/tmp-copy/tool_repl.py /home/jovyan/framework-build/

RUN mkdir -p /opt/conda/share/framework-stubs /opt/conda/share/jupyter/pyrightconfig.d \
    && cd /home/jovyan/framework-build \
    && FRAMEWORK_STUBS_DIR=/opt/conda/share/framework-stubs \
       PYTHONDONTWRITEBYTECODE=1 python /home/jovyan/framework-build/scripts/gen_repl_stubs.py \
    && rm -rf /home/jovyan/framework-build \
    && chown -R ${NB_UID}:${NB_GID} /opt/conda/share/framework-stubs \
    && cat > /opt/conda/share/jupyter/pyrightconfig.d/pyrightconfig.json <<'EOF'
{
  "stubPath": "/opt/conda/share/framework-stubs",
  "extraPaths": ["/home/jovyan/work"],
  "reportUndefinedVariable": "none",
  "reportMissingModuleSource": "none",
  "reportMissingImports": "none",
  "reportAttributeAccessIssue": "none"
}
EOF

RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git\
    && rm -rf /var/lib/apt/lists/*
USER ${NB_UID}
WORKDIR /home/jovyan/work

