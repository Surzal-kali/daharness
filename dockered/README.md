# dockered/ — containerized workbench

A Docker Compose workbench with three services on a `172.28.0.0/24`
`workbench` bridge network: **ChromaDB** (vector registry), **Open WebUI**
(chat front end), and **JupyterLab** (the tool nursery). Everything
bind-mounts the live framework tree — code edits on the host need no
rebuild.

> **History:** an `open-terminal` service (agent shell + file browser that
> hosted the framework gateway + Brain sidecar, port 8000) used to be the
> workbench's core. It was removed (its `open-terminal.Dockerfile` and
> `start_gateway.py` are gone); the gateway and Brain now run on the host
> lane via `python bootstrap.py` / `bootstrap.py --daemon`, and the
> notebook lane reaches the live Brain through the shared socket (see
> below). The stale `open-terminal` comments in `docker-compose.yaml` have
> been cleaned up; one pre-removal env value remains (`FRAMEWORK_GATEWAY_URL`,
> see the note below). The service definitions are the truth.

## Usage

```bash
cd dockered
docker rm -f chroma          # remove a legacy standalone chroma container if present
docker compose up -d --build
```

| Service | Host port | Notes |
| --- | --- | --- |
| ChromaDB | `9000` | mirrors the old standalone `chroma` container; reuses the existing `chroma-data/` volume |
| Open WebUI | `3000` | chat front end; calls the framework via the wrappers in [`../owui-tools/`](../owui-tools/) |
| JupyterLab | `8888` | tool nursery + engagement journals; publishes on all host interfaces (operator choice) |

The framework gateway itself serves **5000 on the host lane** via the host's
`bootstrap.py --daemon` (the `6000` container-lane port predates the
open-terminal removal).

Keys/tokens come from `dockered/.env` (not tracked): `JUPYTER_TOKEN`,
`OPEN_TERMINAL_API_KEY` (now used by Open WebUI → Jupyter integration),
`WEBUI_URL_PREFIX`, `WEBUI_SECRET_KEY`, and — if you expose the gateway to
the wider LAN — `GATEWAY_API_KEY` (host-side `.env`).

## ChromaDB

Mirrored into compose so the workbench is self-contained: same
`chromadb/chroma` image, `chroma-config.yaml` bind-mounted read-only, and
`../chroma-data` reused as the vector store (host port `9000` → container
`8000`). Other services reach it by the DNS name `chroma`
(`CHROMA_HOST=chroma`). Stop the legacy standalone container first —
otherwise you hit a host-port / container-name collision on `chroma`.

## Open WebUI

Chat front end. Notable pins and choices (all documented in the compose
file):

- **Pinned to `:dev`** because the stable `v0.11.3` release breaks the
  interactive terminal shell (the WebSocket proxy omits the
  `X-User-Id`/`X-Session-Id` headers, so PTY attach closes with 4004
  "Session not found"; fix tracked in open-webui/open-webui#29787). Switch
  back to `:main` once a stable release newer than v0.11.3 ships.
- **RAG embedding falls back to Ollama** (`RAG_EMBEDDING_ENGINE=ollama`) —
  the built-in sentence-transformers model loads ~500 MB into RAM on every
  boot. `RAG_EMBEDDING_BATCH_SIZE=50` caps batch spikes.
- **`mem_limit: 4g`** — the OOM kernel killer was shooting the container's
  Python process after ballooning to ~17 GB RSS; a Docker-level limit makes
  the restart deterministic (container restarts via `unless-stopped`)
  instead of the kernel killing a random victim on the host.
- Reaches the framework through the Open WebUI tool wrappers in
  [`../owui-tools/owui-wrapper.py`](../owui-tools/owui-wrapper.py): five
  tools — `framework_search_tools`, `framework_run_tool`,
  `framework_scratch_search`, `framework_memory_search`,
  `framework_health` — with per-chat session isolation. The wrapper's
  `gateway_url` valve **still defaults to `http://open-terminal:6000`**
  (pre-removal); point it at your host gateway (e.g.
  `http://<host>:5000`) for current deployments.

**Approval gate:** the secretary's human-in-the-loop gate is internal to
the harness secretary lane. Tool calls through Open WebUI / the gateway /
MCP execute directly — add your own approval layer in your integrating
harness if you need sign-off there. (The per-turn tool budget in
`utils/tool_budget.py` is the symmetric brake in these lanes.)

## JupyterLab — the tool nursery

Built from `jupyter.Dockerfile` (context is the repo root). Shares the live
framework tree and drives the identical tool lane as the Brain sidecar and
terminal REPL:

- **Bind mounts:** the framework tree → `/home/jovyan/framework` (kernel
  subprocesses run `tool_repl.py` from there), `../dabooks` →
  `/home/jovyan/work` (notebooks, version-controlled on the host — see
  [`../dabooks/README.md`](../dabooks/README.md)), `~/.ipython` persistence,
  and the gitignored `jupyter-nbi/` settings + credential bundle (rebuild-proof).
- **The `daharness` kernel** — installed via
  `python tool_repl.py --install-kernelspec`, preloaded with every
  framework tool as an async wrapper. Notebook cells resolve framework
  tools to bare names (`await session_post(...)`) and drive the same Brain
  session as the terminal REPL, because the kernel runs on the bind-mounted
  tree and the Brain socket (`run/brain.sock`) also lives inside the tree.
  Kernel conventions (always `await`, envelope helpers `keys`/`pluck`/
  `find`/`grep`) are in the Jupyter Kernel section of
  [`AGENTS.md`](../AGENTS.md#jupyter-kernel-dabooksipynb) — the deep dive on
  tool-side behavior lives in
  [`../README.md#tool-repl-tool_replpy`](../README.md#tool-repl-tool_replpy).
- **notebook-intelligence (NBI):** the Dockerfile bakes
  `jupyter-nbi-config.json` (`/opt/conda/share/jupyter/nbi/config.json`,
  bind-mounted read-only) and `patch_nbi_providers.py` /
  `patch_jupyterlab_pypi.py` patch the image's NBI/JupyterLab components.
  `jupyter-nbi/` holds the operator's stored settings + GitHub token
  (contains credentials — gitignored). Notably `chatbook_backend_kernel`
  must be pinned so Chatbook code runs on the `daharness` kernel, not
  `python3`.
- **Language-stub refresh:** `10-refresh-stubs.sh` runs at boot and copies
  the baked `dockered/jupyter-lsp-pyright.json` pyright config into the
  bind-mounted work dir (only if the operator hasn't pinned their own) —
  the jupyterlab-lsp pyright lane gets framework-tool completions.
  Keep `jupyter.Dockerfile`'s baked heredoc and this file in sync if
  editing by hand.
- **Two networks:** `workbench` (the framework lane; reaches `chroma` and
  the host lab lanes) plus an `egress` adapter attached after workbench, so
  it wins the default route and supplies normal NAT'd outbound access (pip
  installs, extension registry). `172.28.0.0/24` still routes framework
  traffic via the workbench interface.
- **Lab DNS:** a kali-side dnsmasq at the pinned bridge gateway
  (`172.28.0.1`) serves the host's `/etc/hosts` lab entries so notebook
  resolution matches the host; Docker's embedded resolver keeps container
  names. Reprovisioned idempotently by `lab-dns-setup.sh` (which also
  writes an `After=docker.service` systemd drop-in); design notes in
  [`../knowledge/lab-dns.md`](../knowledge/lab-dns.md).
- **Healthcheck:** the base image's stock health check always flamped
  "unhealthy" (1s timeout vs ~6s boot — NBI + MCP server + 17 tool
  registrations boot before the API answers), so compose overrides it with
  `/etc/jupyter/docker_healthcheck.py` baked by the Dockerfile.

> **Leftover config note:** the compose service still sets
> `FRAMEWORK_GATEWAY_URL=http://open-terminal:6000` — a pre-removal value
> whose DNS name no longer resolves. The kernel lane dispatches through the
> shared Brain socket rather than the gateway, so notebooks work; update
> this env (or drop it) if you wire notebook-side REST calls to the gateway.

## Network posture

No iptables lockdown / NET_ADMIN cage on any service. Earlier
experiments caged the workbench, but the framework itself is the
scope-lined gateway to the outside world (`check_scope` + the operator-armed
scope gate enforce program scope in-process), and the only thing the cage
actually constrained was the secretary chat loop. Scope discipline is the
in-process layer + operator ROE; the compose file header carries the
full decision record.

## The framework inside these containers

Nothing in this directory runs the framework's gateway or Brain anymore —
the containers consume them:

- the **jupyter kernel** reaches the host Brain via the shared
  `run/brain.sock` (bind-mounted tree);
- **Open WebUI** reaches the gateway over HTTP via `owui-tools/`.

Run the gateway itself on the host (`python bootstrap.py --daemon`) — or,
for a hosted deployment, point the wrappers/valves wherever the gateway
actually serves.