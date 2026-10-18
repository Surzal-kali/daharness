#!/usr/bin/env bash
# code-server boot-time provisioning (idempotent — mounted into the
# linuxserver image's /custom-cont-init.d hook, runs before code-server
# starts on every boot).
#
# Marketplace discipline: OpenVSX only (code-server's default gallery).
# NO Microsoft-gallery side-loading, NO Copilot VSIX — the marketplace-ToU
# gray zone stays out of this lane. The assistant is continue.continue
# pointed at the host-lane Ollama, so completions/chat stay on the LAN.
#
# Failures are non-fatal by design: the editor still boots, and failed
# installs retry on the next boot.

set -u

EXTS=(
  ms-python.python
  detachhead.basedpyright
  llvm-vs-code-extensions.vscode-clangd
  continue.continue
)

# Paths mirror the lsio s6 service invocation (svc-code-server):
# --user-data-dir /config/data --extensions-dir /config/extensions
CS_BIN="$(command -v code-server || echo /app/code-server/bin/code-server)"
CS_DATA_DIR=/config/data
CS_EXT_DIR=/config/extensions
OLLAMA_BASE="${OLLAMA_BASE_URL:-http://192.168.56.1:11434}"

# The Extensions VIEW has no registry wired up unless product.json carries
# an "extensionsGallery" block — coder ships this build WITHOUT one, so the
# marketplace renders dead even though open-vsx.org is reachable (the CLI
# install path has its own OpenVSX default, which is why boot provisioning
# never noticed). Inject it before the server starts. Node is bundled with
# code-server at /app/code-server/lib/node — NOT "bin/../lib/node": the
# code-server shim is a file, so that path trips node's argv[0]
# self-resolution ("Not a directory").
PRODUCT_JSON=/app/code-server/lib/vscode/product.json
NODE_BIN=/app/code-server/lib/node
[ -x "$NODE_BIN" ] || NODE_BIN="$(command -v node || true)"
if [ -n "$NODE_BIN" ] && [ -f "$PRODUCT_JSON" ]; then
  # node -e with the path as a trailing arg — `node -` does NOT mean
  # "read stdin"; node treats it as a filename (ENOENT).
  if ! "$NODE_BIN" -e 'const fs=require("fs");const path=process.argv[1];const data=JSON.parse(fs.readFileSync(path,"utf8"));if(data.extensionsGallery){console.log("[code-server-init] extensionsGallery already set; skipping")}else{data.extensionsGallery={serviceUrl:"https://open-vsx.org/vscode/gallery",itemUrl:"https://open-vsx.org/vscode/item"};fs.writeFileSync(path,JSON.stringify(data,null,2));console.log("[code-server-init] injected extensionsGallery (open-vsx) into product.json")}' "$PRODUCT_JSON"; then
    echo "[code-server-init] WARN: gallery injection into product.json failed (non-fatal; UI marketplace stays offline this boot)"
  fi
else
  echo "[code-server-init] WARN: bundled node or product.json not found; skipping gallery injection (non-fatal)"
fi

# Give the registry a short grace window so one slow first boot doesn't
# leave all four installs failed.
network_ok=0
for _ in $(seq 1 10); do
  if curl -fsSm 5 https://open-vsx.org/ >/dev/null 2>&1; then
    network_ok=1
    break
  fi
  sleep 3
done
if [ "$network_ok" -ne 1 ]; then
  echo "[code-server-init] openvsx unreachable at boot - skipping installs, editor will still start"
  exit 0
fi

export HOME=/config
for ext in "${EXTS[@]}"; do
  if ! "$CS_BIN" --user-data-dir "$CS_DATA_DIR" --extensions-dir "$CS_EXT_DIR" \
      --disable-telemetry --install-extension "$ext" >/dev/null 2>&1; then
    echo "[code-server-init] WARNING: ${ext} install failed (non-fatal; retries next boot)"
  fi
done

# Continue -> host-lane Ollama. Only written when absent so operator edits
# (models, endpoints, rules) survive every reboot; tune in the Continue UI
# or edit the file by hand and it stays.
mkdir -p /config/.continue
if [ ! -s /config/.continue/config.yaml ]; then
  cat >/config/.continue/config.yaml <<EOF
name: Ollama (LAN)
version: 0.0.1
models:
  - name: qwen2.5-coder 7b - autocomplete
    provider: ollama
    model: qwen2.5-coder:7b
    apiBase: ${OLLAMA_BASE}
    roles:
      - autocomplete
    defaultCompletionOptions:
      contextLength: 8192
  - name: qwen2.5-coder 14b - chat/edit
    provider: ollama
    model: qwen2.5-coder:14b
    apiBase: ${OLLAMA_BASE}
    roles:
      - chat
      - edit
      - apply
EOF
  echo "[code-server-init] provisioned /config/.continue/config.yaml (Ollama: ${OLLAMA_BASE})"
fi

# code-server writes its default config at ANY first launch — including our
# CLI install calls — so treat it as a base we may upgrade, not an absent
# file: flip cert to self-signed TLS unless an operator already set it.
mkdir -p /config/.config/code-server
if [ ! -s /config/.config/code-server/config.yaml ]; then
  printf 'auth: password\ncert: true\n' >/config/.config/code-server/config.yaml
  echo "[code-server-init] provisioned /config/.config/code-server/config.yaml (cert: true)"
elif ! grep -q '^cert: true$' /config/.config/code-server/config.yaml; then
  sed -i 's/^cert: false$/cert: true/' /config/.config/code-server/config.yaml
  echo "[code-server-init] upgraded /config/.config/code-server/config.yaml (cert: true)"
fi

# continue.continue may resolve to an older release for engine compatibility
# (OpenVSX served 2.0.0 for this code-server) — older builds read the legacy
# config.json, newer read config.yaml. Provision both, only-if-absent, so
# every supported version finds the Ollama lane.
if [ ! -s /config/.continue/config.json ]; then
  cat >/config/.continue/config.json <<EOF
{
  "tabAutocompleteModel": {"title": "qwen2.5-coder 7b", "provider": "ollama", "model": "qwen2.5-coder:7b", "apiBase": "${OLLAMA_BASE}"},
  "models": [{"title": "qwen2.5-coder 14b - chat/edit", "provider": "ollama", "model": "qwen2.5-coder:14b", "apiBase": "${OLLAMA_BASE}"}]
}
EOF
  echo "[code-server-init] provisioned /config/.continue/config.json (legacy Continue schema)"
fi

# The lsio init chown (init-code-server) runs BEFORE this script, so fix up
# our own writes for the service user (abc == PUID/PGID of this container).
OWNERSHIP="${PUID:-1000}:${PGID:-1000}"
chown -R "${OWNERSHIP}" "$CS_DATA_DIR" "$CS_EXT_DIR" /config/.continue /config/.config /config/.local 2>/dev/null || true