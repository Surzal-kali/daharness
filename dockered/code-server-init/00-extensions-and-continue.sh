#!/usr/bin/env bash
# code-server boot-time provisioning (idempotent — mounted into the
# linuxserver image's /custom-cont-init.d hook, runs before code-server
# starts on every boot).
#
# Marketplace discipline (revised 2026-10-18): the gallery injected below is
# the Microsoft one. Reason: OpenVSX builds of ms-python are stripped of the
# bundled native interpreter-discovery binary (python-env-tools "pet") —
# every ms-python vsix on OpenVSX lacks it, so interpreter discovery dies
# with `spawn .../python-env-tools/bin/pet ENOENT` and Select Python
# Interpreter can never see /opt/framework-venv. The Microsoft marketplace
# vsix bundles pet and is what the code-server ecosystem at large runs. The
# earlier OpenVSX-only stance existed to keep the marketplace-ToU gray zone
# (the Copilot VSIX footwork) out of this lane; with Copilot resolving at
# init today that rationale lapsed. Still NO Copilot VSIX: the assistant
# stays continue.continue → host-lane Ollama, so completions/chat remain
# local-network only. The editor's trust posture is otherwise unchanged.
#
# Failures are non-fatal by design: the editor still boots, and failed
# installs retry on the next boot.

set -u

EXTS=(
  ms-python.python
  ms-python.vscode-python-envs
  ms-toolsai.jupyter
  detachhead.basedpyright
  llvm-vs-code-extensions.vscode-clangd
  continue.continue
)
# ms-python.vscode-python-envs is the interpreter-management backend
# ms-python 2026.x drives for Select Interpreter; explicit so the boot state
# is deterministic rather than trusting dependency auto-resolution.
# ms-toolsai.jupyter is what actually attaches .ipynb to kernels — with it,
# the image's baked daharness kernelspec (see dockered/code-server.Dockerfile)
# shows up in the kernel picker and notebook cells run the framework lane.

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
  if ! "$NODE_BIN" -e 'const fs=require("fs");const path=process.argv[1];const data=JSON.parse(fs.readFileSync(path,"utf8"));const gallery={serviceUrl:"https://marketplace.visualstudio.com/_apis/public/gallery",itemUrl:"https://marketplace.visualstudio.com/items"};if(data.extensionsGallery&&data.extensionsGallery.serviceUrl===gallery.serviceUrl){console.log("[code-server-init] extensionsGallery already set; skipping")}else{data.extensionsGallery=gallery;fs.writeFileSync(path,JSON.stringify(data,null,2));console.log("[code-server-init] injected extensionsGallery (microsoft marketplace) into product.json")}' "$PRODUCT_JSON"; then
    echo "[code-server-init] WARN: gallery injection into product.json failed (non-fatal; UI marketplace stays offline this boot)"
  fi
else
  echo "[code-server-init] WARN: bundled node or product.json not found; skipping gallery injection (non-fatal)"
fi

# ── TLS: internal CA + signed leaf with host SANs ───────────────────────────
# code-server's auto-generated self-signed cert only covers DNS:localhost.
# Webview service workers REFUSE to register on any cert error (no browser
# click-through), so browsing via a LAN IP (10.0.0.42) fails webviews even
# with the cert trusted. Design: one internal CA (10-year) signs an 825-day
# leaf serving the actual host addresses. Devices install the CA ONCE —
# leaf rotations/volume wipes then never break trust. CA lives in the
# volume; `docker cp` ca.crt to devices.
CERT_DIR=/config/.local/share/code-server
CA_DIR="$CERT_DIR/ca"
CA_KEY="$CA_DIR/ca.key"
CA_CRT="$CA_DIR/ca.crt"
# SANs: explicit via CODE_SERVER_CERT_SANS (compose env, host IPs are
# per-machine), else best-effort from `hostname -I`, else DNS-only (webview
# over a raw LAN IP will still fail — set the env).
SAN_EXTRA="${CODE_SERVER_CERT_SANS:-}"
if [ -z "$SAN_EXTRA" ] && command -v hostname >/dev/null 2>&1; then
  SAN_EXTRA="$(hostname -I 2>/dev/null | tr ' ' '\n' | sed '/^$/d' | grep -v '^127\.' | sed 's/^/IP:/' | paste -sd, -)"
fi
SAN="DNS:localhost,DNS:kali,IP:127.0.0.1"
[ -n "$SAN_EXTRA" ] && SAN="${SAN},${SAN_EXTRA}"
mkdir -p "$CA_DIR"
if [ ! -s "$CA_KEY" ] || [ ! -s "$CA_CRT" ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
    -keyout "$CA_KEY" -out "$CA_CRT" \
    -subj "/CN=kali-code-server-ca" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1 \
    && chmod 600 "$CA_KEY" \
    && chown -R "${PUID:-1000}:${PGID:-1000}" "$CA_DIR" \
    && echo "[code-server-init] minted internal CA — install ca/certs on devices ONCE, leaf rotations stay trusted"
fi
# (Re)issue the served leaf when absent OR not signed by our CA (migrates
# old self-served leaves transparently on upgrade).
if [ ! -s "$CERT_DIR/localhost.crt" ] || [ ! -s "$CERT_DIR/localhost.key" ] \
   || ! openssl verify -CAfile "$CA_CRT" "$CERT_DIR/localhost.crt" >/dev/null 2>&1; then
  openssl req -newkey rsa:2048 -nodes \
    -keyout "$CERT_DIR/localhost.key" -out "$CA_DIR/leaf.csr" \
    -subj "/CN=kali-code-server" >/dev/null 2>&1 \
    && printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\n" "$SAN" > "$CA_DIR/leaf.ext" \
    && openssl x509 -req -in "$CA_DIR/leaf.csr" -CA "$CA_CRT" -CAkey "$CA_KEY" \
       -CAcreateserial -days 825 -extfile "$CA_DIR/leaf.ext" \
       -out "$CERT_DIR/localhost.crt" >/dev/null 2>&1 \
    && rm -f "$CA_DIR/leaf.csr" "$CA_DIR/leaf.ext" \
    && chmod 600 "$CERT_DIR/localhost.key" \
    && chown "${PUID:-1000}:${PGID:-1000}" "$CERT_DIR/localhost.key" "$CERT_DIR/localhost.crt" \
    && echo "[code-server-init] leaf reissued under internal CA (SAN: ${SAN})"
fi

# Give the registry a short grace window so one slow first boot doesn't
# leave all installs failed.
network_ok=0
for _ in $(seq 1 10); do
  if curl -fsSm 5 https://marketplace.visualstudio.com/ >/dev/null 2>&1; then
    network_ok=1
    break
  fi
  sleep 3
done
if [ "$network_ok" -ne 1 ]; then
  echo "[code-server-init] gallery (marketplace) unreachable at boot - skipping installs, editor will still start"
  exit 0
fi

export HOME=/config

# Install Starship into the persistent config volume so image upgrades do not
# remove the prompt. The shell hook is written separately so a failed download
# never prevents the editor from starting.
STARSHIP_BIN=/config/.local/bin/starship
mkdir -p /config/.local/bin /config
if [ ! -x "$STARSHIP_BIN" ]; then
  if curl -fsSL https://starship.rs/install.sh | sh -s -- --yes --bin-dir /config/.local/bin >/dev/null 2>&1; then
    echo "[code-server-init] installed Starship prompt"
  else
    echo "[code-server-init] WARNING: Starship install failed (non-fatal; retries next boot)"
  fi
fi
if [ -x "$STARSHIP_BIN" ]; then
  touch /config/.bashrc
  if ! grep -Fq 'starship init bash' /config/.bashrc; then
    printf '\n# Starship prompt\nexport PATH="/config/.local/bin:$PATH"\neval "$(starship init bash)"\n' >>/config/.bashrc
  fi
fi

for ext in "${EXTS[@]}"; do
  if ! "$CS_BIN" --user-data-dir "$CS_DATA_DIR" --extensions-dir "$CS_EXT_DIR" \
      --disable-telemetry --install-extension "$ext" >/dev/null 2>&1; then
    echo "[code-server-init] WARNING: ${ext} install failed (non-fatal; retries next boot)"
  fi
done

# Repair for pre-gallery-swap boots: the OpenVSX ms-python build is stripped
# of the bundled python-env-tools binary (the interpreter picker's discovery
# engine — without it Select Python Interpreter is dead). --force reinstalls
# from the Microsoft gallery WITHOUT uninstalling first, so a failed fetch
# leaves the working-but-stripped extension in place until a later boot.
# sort -V: lexical glob order sorts 2026.10.0 < 2026.4.0 — the highest
# version must win, and stale uninstall leftovers must lose.
PET_DIR="$(ls -d "${CS_EXT_DIR}"/ms-python.python-*/ 2>/dev/null | sort -V | tail -1)"
if [ -n "$PET_DIR" ] && [ ! -x "${PET_DIR}python-env-tools/bin/pet" ]; then
  echo "[code-server-init] ms-python build lacks python-env-tools (stripped OpenVSX artifact) — forcing marketplace reinstall"
  for ext in ms-python.python ms-python.vscode-python-envs; do
    "$CS_BIN" --user-data-dir "$CS_DATA_DIR" --extensions-dir "$CS_EXT_DIR" \
      --disable-telemetry --install-extension "$ext" --force >/dev/null 2>&1 \
      || echo "[code-server-init] WARNING: ${ext} marketplace reinstall failed (non-fatal; retries next boot)"
  done
  # Re-glob: a --force install may land a newer version dir than PET_DIR.
  PET_DIR="$(ls -d "${CS_EXT_DIR}"/ms-python.python-*/ 2>/dev/null | sort -V | tail -1)"
fi
if [ -n "$PET_DIR" ] && [ -x "${PET_DIR}python-env-tools/bin/pet" ]; then
  echo "[code-server-init] python-env-tools binary present (interpreter discovery operational)"
fi

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
chown "${OWNERSHIP}" /config/.bashrc 2>/dev/null || true