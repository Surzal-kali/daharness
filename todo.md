# TODO — Framework Next Steps (updated Sept 18, 2026)

> After adding ANY new tool: re-run bootstrap/reindex so the registry embeds it —
> undiscovered tools are invisible to the secretary (see memory_tools lesson).
>
> Cleanup Sept 15: completed phase items and the landed T-001..T-004 tickets
> (all four landed in 0529b4c, verified) removed. Open acceptance criteria and
> genuinely unfinished work retained below.

- [x] **SSRF scanner** LANDED: `auxiliaries/ssrf_probe.py` → `scan_ssrf`.
      Parametric SSRF fuzzer/probe (OWASP Top 10 now has its own scanner like
      the rest). Payload battery: OOB/collaborator (blind, hard confirm),
      localhost variants, internal RFC1918 + cloud metadata (169.254.169.254,
      metadata.google.internal), scheme bypasses (file/gopher/dict/ftp/ldap).
      Detection: blind collab callback correlation; in-band metadata/file
      reflection; fetch-error-string leak; status/length/timing anomaly vs
      baseline (inferential, flagged honestly). Target endpoint scope-gated
      (payload values intentionally NOT gated — they are what the server
      fetches, not our traffic). Uses new `utils.gated_http.gated_request`
      (method+body, per-hop gated). Pair with collab_start + zap_send_raw.
      Note: redirect-to-internal payloads deferred (collaborator has no
      redirect endpoint yet) — add a /redirect path to listeners/collaborator
      to unlock.

## Open acceptance criteria (from landed phases)
- [ ] e2e runner ends every chain with ≥1 Finding; render_findings output saved
      as an e2e artifact (Phase 1 acceptance)
- [ ] Blind SSRF/XSS lab test for collaborator: inject collab URL, trigger,
      collab_poll shows the callback with matching qname ID (Phase 3 acceptance)

## Recon leftovers
- [ ] subfinder as optional secondary wrapper (deferred; amass covers v1 + brute
      + alts — revisit only if a real gap appears)
- [ ] Profile filtering: bounty profile (nmap, subfinder, ZAP, repeater, memory,
      report) vs lab profile (everything) — verify whether registry already does
      this before building
- [ ] Map Finding severity to per-program severity scales (H1 ranges, Intigriti
      CVSS 0.1-10, Adobe Tier 1-3) for render_findings drafts

## Browser & JS surface (bug bounty + career lane)
- [ ] **static JS route extractor** (BUILD FIRST — cheap 80%, no browser):
      fetch .js bundles + regex endpoints/API routes (linkfinder-style),
      ~50-line auxiliary, zero heavy deps, sandbox-workable today.
- [x] **playwright integration** LANDED (desktop/framework host ONLY — phone sandbox
      is permanently out: Sept 13 install got the app killed; standing rule).
      Sidecar: `auxiliaries/playwright_sidecar.py` (loopback HTTP API, env-gated
      launch PLAYWRIGHT_SIDECAR=1, bootstrap-wired like ZAP). Client:
      `auxiliaries/playwright_recon.py` — `playwright_fetch` (one-shot rendered-
      DOM envelope: final URL, status, title, text, links, forms, JS-discovered
      routes, blocked_requests, challenge_detected) + `playwright_crawl` /
      `playwright_crawl_status` / `playwright_crawl_stop` (launch/poll, nmap
      pattern, bounded pages/depth/time). Scope enforced at the BROWSER REQUEST
      ROUTING LAYER via context.route: navigations + fetch/XHR/websocket are
      check_scan-gated and aborted out-of-scope; PASSIVE subresources (img/css/
      font/media/script) allowed from anywhere so pages render (operator policy
      2026-09-21: strict subresource gating breaks most real pages). Live
      scope read (mtime-cached) so a REPL scope change takes effect next
      request. Auth'd crawling via persisted storageState (env-injected only).
      challenge_detected honest flag, never faked. Vanilla only — no stealth
      patches. Install: `pip install playwright` + `python -m playwright install
      chromium` (done on this host). Verified: armed Cloudflare scope correctly
      blocked an OOS example.com navigation and logged it in blocked_requests.
      Use case: JS-heavy bounty surfaces (SPA recon, auth'd crawling,
      bugbounty-arsenal.net), UNMJobs/Workday-class form flows (career lane).

## Phase 4 — Credential motion (lab chain)
- [x] **Responder analog v1 LANDED** (`listeners/responder_analog.py`, Oct
      2026): CAPTURE-ONLY LLMNR (UDP 5355) + NBT-NS (UDP 137) poisoner +
      self-contained SMB2 sink (dialect 2.0.2, NBSS framing incl. 0x81
      session requests, SPNEGO-wrapped NTLMSSP challenge with realistic
      MS-NLMP TargetInfo; exactly one auth per capture, then
      STATUS_LOGON_FAILURE — no relay code path). Captures →
      run/responder/captures.jsonl (0600) + `remember_text` ns 'responder'
      + hashcat -m 5600 / john netntlmv2 lines. Lab-gated via
      SHAPING_LAB_MODE=1 (same gate as decoy/fragment presets); TCP 445
      capability probe with setcap remediation; own hostname + wpad
      blacklisted; LLMNR answers A-only. Integration-proved against a REAL
      impacket client (loopback): negotiate → challenge → type-3 → captured
      hash line. MSF-capture-route rejected by operator (msf hardcodes
      modules in unaware ways) — sink stays in-tree.
- [ ] GPP cpassword tool: smb_read_file Groups.xml + decrypt cpassword (published
      AES key); hint chain: smb_scanner null session -> gpp_cpassword
- [ ] samrdump / lookupsid wrappers (SAMR/SID enum) feeding target lists
- [x] Next hints: pass-the-hash flow documented in descriptions —
      secretsdump -hashes :<NTLM> -> psexec_exec (LANDED impacket_suite.py:259)
- [ ] Acceptance: poison -> capture -> run_hashcat 5600 -> secretsdump
      -hashes -> psexec_exec, fully secretary-driven (needs live lab VM +
      re-index of the new responder tools)

## Parked — do NOT implement until covered in coursework
- [ ] GetNPUsers (AS-REP roast) — parked Sept 8
- [ ] ntlmrelayx (relay) — parked Sept 8; coursework will NOT cover it
      (operator ruling Oct 2026): mechanics refresher delivered (chat,
      Oct 2026) — proxy-the-auth-model, one-leg targeting matrix
      (SMB/HTTP in → LDAP/LDAPS/MSSQL/ADCS out), 24H2 required-signing
      reality, coercion lane, per-target allowlist invariant. Build only
      with a live lab + explicit operator blessing.
- [ ] dpapi (blob decryption) — parked, BUT coursework coverage unlikely
      (operator ruling Oct 2026): mechanics refresher delivered. Shape when
      built: thin `impacket.dpapi` library wrapper (library CONFIRMED in
      venv; examples script absent) — decode/masterkey-password/
      masterkey-backup/credential tools; all OFFLINE (routes: user
      password+SID / DPAPI_SYSTEM via secretsdump / DC domain backup key);
      chains with secretsdump + smb_read_file; no gates needed. Chrome 127+
      app-bound cookies = honest wall.

## Backlog / big rocks
- [ ] radare2 composite wrapper: r2 subprocess, command allowlist
      (aaa, afl, pdf, iz, axt...), r2ghidra for decompile — THE big work-up
- [ ] SET integration
- [ ] Vector search over sanitized module descriptions (Qdrant or 2nd ChromaDB
      collection) — the scaling lever for thousands of modules
- [ ] MCP layer (radare2/Burp/Ghidra): one gateway tool per server, curated
      subsets only — no wholesale registration
