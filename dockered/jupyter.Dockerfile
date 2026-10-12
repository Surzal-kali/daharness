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

COPY requirements.txt /tmp/framework-requirements.txt

RUN python -m pip install --no-cache-dir -r /tmp/framework-requirements.txt \
    && python -m pip install --no-cache-dir \
        chromadb==1.5.9 \
        pydantic-ai==2.35.0 \
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

USER ${NB_UID}
WORKDIR /home/jovyan/work

