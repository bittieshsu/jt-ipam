# jt-ipam Release Test Checklist

> zh-TW version: [TEST_CHECKLIST_zh-TW.md](TEST_CHECKLIST_zh-TW.md).

> Rule: **before bumping the `version` in `frontend/package.json`, run this whole
> checklist once; only release when everything is green.**
> Treat it as the manual gate. Fix the red ones first — do not ship sick.

Release flow: run the checklist → all green → bump version → deploy
(backend rsync + alembic + restart; frontend build).

---

## 1. Static checks (dev box, no DB, fastest)

- [ ] Backend imports: `cd backend && set -a; source <env>; set +a; .venv/bin/python -c "import app.main"`
- [ ] Backend pytest collection has no error (DB tests skip): `.venv/bin/pytest -q`
- [ ] Frontend types: `cd frontend && npx vue-tsc --noEmit` (must be zero errors)
- [ ] Frontend build: `npm run build` (dist produced successfully)
- [ ] i18n: every new key exists in both `zh-TW.json` and `en-US.json`; no hard-coded
  Chinese slipped through

## 2. Database / migration (use a throwaway test DB, never touch prod data)

- [ ] A fresh DB upgrades from 0001 to head cleanly: run `alembic upgrade head`
  against `jt_ipam_test`
- [ ] Each new migration has a `downgrade()` and survives one
  `alembic downgrade -1` then `upgrade head` round-trip
- [ ] No "model changed but migration forgotten": after upgrading to head the app
  starts without an asyncpg "column does not exist" error
- [ ] **Constraint changes**: when a migration drops or adds a UNIQUE constraint, audit
  every query that relied on it. `scalar_one_or_none()` on a column that can now repeat
  becomes a 500 the moment a second row appears (v0.5.194: `users.email`), and code that
  wrote the column unconditionally starts hitting IntegrityError

## 3. Backend integration tests (test DB + pytest, thorough)

- [ ] With `JTIPAM_TEST_DATABASE_URL` set, `.venv/bin/pytest -q` is all green
  (e2e CRUD / auth / each module)
- [ ] Auth: login, refresh, TOTP, permissions (unauthorized `require_admin`
  endpoints return 401/403)
- [ ] Core CRUD: sections / subnets / addresses / devices / customers / locations / racks
- [ ] Audit chain: write operations are audited and chain integrity verifies

## 3b. Authentication realms & account identity

Login spans local / LDAP / RADIUS / OIDC / SAML, and the same human legitimately owns
accounts in more than one of them. Every defect here reaches the user as "I cannot log in",
with the real cause hidden in a traceback.

- [ ] Log in through **every configured realm**; a wrong password returns 401 with a generic
  message (no account enumeration) while the server log records the specific reason
- [ ] **Same person, two realms**: a local account and an LDAP/SSO account sharing one email
  both log in, and neither overwrites the other's row (v0.5.194: the shared email hit the
  UNIQUE index and returned 500 *after* the LDAP bind had already succeeded)
- [ ] **Auto-provisioning**: first external login creates the account, second updates it;
  a collision on any unique column degrades gracefully instead of failing the login
- [ ] Lockout after repeated failures, then unlock; a deactivated account is refused
- [ ] Logging in by email (not username) resolves to exactly one account per realm

## 4. Key API smoke (against prod after deploy, mostly read-only)

- [ ] `GET /api/v1/health` (or `/notifications`) returns 200
- [ ] `GET /api/v1/subnets`, `/addresses`, `/devices`, `/locations`, `/racks` return 200
- [ ] Endpoints touched this release: manually hit one success path + one failure
  path (verify the 4xx is correct)

## 5. OWASP Top 10:2025 self-review (modules touched this release)

- [ ] A01 authorization: do new endpoints correctly use `require_admin` /
  object-level authorization?
- [ ] A03 injection / input validation: Pydantic StrictModel; file uploads verify
  magic bytes + size limit + reject dangerous types (e.g. SVG)
- [ ] A08 integrity: uploads / external data are validated; no path traversal
  (resolved upload/download paths stay inside the allow-listed directory)
- [ ] Secrets: no secret/token written to logs or responses

## 5b. Deploy-script flows (throwaway environment, **never run install on dev/prod**)

Every install problem customers have reported was invisible on an already-working
box, because there the thing is already there: a pre-existing PostgreSQL cluster
on a different major (so `pgvector` got installed for the wrong one), a `pnpm
install` that failed silently and left no frontend, an installer that printed
"Done" while nothing was running, and a backup unit whose `ReadWritePaths`
directory did not exist yet — which systemd reports as `226/NAMESPACE`, an error
that names nothing about the actual cause. **A clean-OS install is the only way
to see what a customer sees.**

- [ ] **Fresh install from a clean OS — required**: `scripts/test-fresh-install.sh
  debian:12` exits 0. It starts a throwaway systemd container, copies the tree in,
  runs `scripts/jt-ipam.sh install`, then checks the things that only break in the
  field: the backend *answers* on its port, `jt-ipam-backup` and `jt-ipam-sync`
  actually run to `Result=success`, the backup unit survives its directory being
  deleted, and `doctor` agrees with reality
- [ ] Run it for **the oldest supported distro and the newest** (`debian:12`,
  `ubuntu:24.04`); PG-major and Node-version differences live there
- [ ] **Upgrade**: against a previous-version environment run
  `scripts/jt-ipam.sh upgrade`; it upgrades cleanly and can roll back if needed
- [ ] If this release added a directory / package / service / DB extension / env,
  confirm **`install` and `upgrade` are both in sync** — and that `doctor` checks it
- [ ] **`scripts/jt-ipam.sh doctor` on prod after deploying**: every line green, or
  the `→ fix` line is one a customer could follow without asking us
- [ ] **(A) Default admin credentials**: fresh install prints the `admin` account +
  random password at the end and saves it to `/etc/jt-ipam/.admin-initial-password`
  (root 0600); that password logs in
- [ ] **(A) Password-reset CLI**: `python -m app.cli.bootstrap create-admin --username
  admin --password-stdin --force-update` resets an existing admin; both READMEs
  document it
- [ ] **(B) Agent probe tools**: after `agent/jt-ipam-agent-installer.sh`, the host has
  `nmap` / `nmblookup` (samba-common-bin) / `avahi-resolve` (avahi-utils); the agent's
  reported `available_probes` includes os/netbios/mdns
- [ ] **(B) Install-help UI**: on the scan-agent page and the subnet edit dialog,
  unavailable probes show an "install help" popover with the matching install command

## 5c. Real-browser testing — **mandatory for every release that touches the UI**

Type checks, unit tests and API tests all pass while a page renders the wrong
thing, renders nothing, or puts it in the wrong place. Defects this project has
shipped that were only ever visible in a browser: a column added to a table but
not to the column-picker defaults (so it never appeared), an export that wrote
`undefined` into the report, a date overlapping its buttons, file names that
failed to line up by 16px, and a console that could not connect at all because
the reverse proxy dropped the WebSocket upgrade.

- [ ] `cd frontend && pnpm exec playwright test smoke` (no backend; self-starts
  vite preview) all green
- [ ] Against a deployed instance (`E2E_BASE_URL` + `E2E_ADMIN_PASS`) run the
  **whole** suite: `pnpm test:e2e`. Data-dependent specs need real data — run those
  against a deployed instance, not an empty test DB
- [ ] **Every changed page opened in an actual browser**, console watched: no errors,
  no blank regions, no `undefined` / raw JSON / untranslated i18n keys on screen
- [ ] **A new spec covering what this release changed.** Assert on the effect, not on
  the UI's own claim: read the file back off the remote host, reload the page after
  saving, compare the downloaded bytes. "已上傳" on screen is not evidence
- [ ] **Measure geometry, don't eyeball it** — `boundingBox()` whenever the point is
  alignment, overlap or spacing; a screenshot hides a 16px error
- [ ] New text checked in both locales (switch to English, confirm no key leaks)

## 5d. System export / import (cross-instance migration) — **run in full every release that touches it**

- [ ] **Unit (no DB)**: `pytest tests/test_system_transfer.py -q` — crypto seal/open
  (wrong passphrase → readable error, not 500), secrets round-trip for every
  representation (column / central / envelope / settings-blob), `registry.validate_registry()`
  returns empty (every table categorised), backward-compat coercion drops unknown columns
- [ ] **DB-backed** (`JTIPAM_TEST_DATABASE_URL` at head): export→import round-trip
  preserves UUIDs + FKs, secrets re-decrypt under the target key, `merge` is idempotent
  (2nd run all `updated`, no dup rows), `replace` wipes first, `dry_run` writes nothing
- [ ] **Backward compat**: an older/reduced export file (missing newer tables/columns)
  imports without error; the target schema_version mismatch shows a warning, not a failure
- [ ] **CLI**: `python -m app.cli.system_transfer export --scope … --out f.json --passphrase-stdin`
  then `import --file f.json --dry-run` then real `import`; counts correct, wrong
  passphrase exits non-zero
- [ ] **UI (admin → System Export / Import)**: pick scope + passphrase → generate →
  download; upload on a second instance → analyze (shows source version + counts +
  warnings) → dry-run preview → apply (merge and replace); non-admin gets 403 / no menu
- [ ] **End-to-end migration**: export full default scope from instance A, import into a
  clean instance B, then log in on B and confirm subnets / IP / devices / integrations
  are present, an integration actually connects (secret re-encrypted), SSH credential
  works, and TOTP still logs in
- [ ] **Security**: download / analyze / apply all require admin + validate task ownership;
  spool files are 0600 in a 0700 dir; no plaintext secret or passphrase in logs/responses

## 5e. AI chat / MCP tools — **every release that touches tools, prompts, or the data they read**

Wrong AI answers do not look wrong: every number in them is real, just computed over the
wrong set. Unit tests pass because each tool returns exactly what it was asked for — the
defect is in *what the model was able to ask*.

- [ ] **Scope**: for each tool returning per-object data, ask a question naming one subnet /
  rack / location and confirm the answer contains only that scope. Regression to guard:
  "which hosts in 198.51.100.0/24 have no Wazuh agent" answered with the whole system
  because the tool had no subnet parameter at all (v0.5.194)
- [ ] **Schema exposes the scope**: the tool description tells the model it MUST pass the
  scope for a scoped question, and the reply carries `scope` so the answer can state coverage
- [ ] **No silent truncation**: every list tool returns `count` (total in scope) next to
  `returned`; ask something exceeding `limit` and confirm the answer says it is a partial list
  instead of presenting one page as the total
- [ ] **Permission tiers**: each new/changed tool sits in the right tier (mutating / admin /
  global-read / per-object) and `allowed_tool_names()` hides it from accounts that cannot call
  it. Verify through the actual AI chat with a restricted account, not only in unit tests
- [ ] **Read-only stays read-only**: analysis/triage tools never write, never notify, never commit
- [ ] **Prompt injection**: attacker-controlled text (mDNS hostname, firewall rule description)
  stays fenced and truncated; the adversarial tests still pass
- [ ] **Facts come from tools, not arithmetic**: usage / free / count answers are fetched, never
  computed by the model from a CIDR

- [ ] **Cancellable**: while generating, Send becomes Stop; pressing it aborts the request
  (closing the connection also stops the LLM server), and the transcript says it stopped
- [ ] **Progress is visible**: connecting / thinking / which tool / composing / answering, each
  with the round number and elapsed seconds — **a bare spinner is indistinguishable from a hang**
- [ ] **An empty reply is never passed through**: the model is asked once more for a direct
  answer, and if it is still empty the reason is stated (length limit vs no text at all)
## 5f. Browser consoles (SSH / BMC / PVE) — **whenever the terminal changes**

- [ ] **URLs are clickable**: when a TUI has broken a long URL across rows
  (`printf '%s\n' "$URL" | fold -w $(tput cols)` reproduces it), hovering the **second row**
  still recognises the whole URL, and the bar shows the full target
- [ ] **Only http/https open**, in a new tab with no opener (the text comes from the remote host)
- [ ] **Selection copy**: a URL split across rows copies as one usable address;
  **ordinary multi-line text must be left untouched**
- [ ] **No false joins**: a full-width line followed by unrelated text is not glued into a URL
- [ ] **SFTP sort mode**: with Folders first, directories lead in **both ascending and descending**
  order (putting the grouping inside the comparator inverts it on descending — that is the regression
  to watch); Mixed sorts purely by the column. Sorting by size or mtime honours the same mode.
  The choice is saved to user preferences and survives a reconnect or a different device
- [ ] Existing spec: `frontend/e2e/terminal-links.spec.ts` (needs `E2E_SSH_ADDRESS_ID/USER/PASS`,
  plus `can_ssh` on the user and `ssh_enabled` on the address; accept the host key on first use)

## 6. Manual page review (browser, after deploy)

- [ ] Login / logout / theme switch (light / dark / auto)
- [ ] Subnets: list, tree, IP list (incl. idle-range rows spanning columns), edit
- [ ] Devices / racks: sorting (natural IP order), consistent action-button height,
  floor-plan upload + drag-to-place + select
- [ ] Topology: nodes / links, VPN pairing links, legend
- [ ] Scan agents / sync jobs: pages render, no console errors

## 7. pfSense integration (Admin → 整合 pfSense)

> Prereq on the pfSense (CE 2.8.x): install **pfSense-pkg-RESTAPI** (pfrest.org), then System →
> REST API → Settings add **"API Key"** to auth methods, and create a key under Keys.

- [ ] Add instance: API URL + X-API-Key, **Verify TLS off** for a self-signed cert; save (key write-only, never returned).
- [ ] **Test connection** → success with the pfSense version.
- [ ] **Sync now** (ARP + aliases + rules on; **DHCP off** if another DHCP server owns the LAN) → counts return; an
  in-scope ARP IP gets `last_seen` (source `pfsense`) + MAC; aliases/rules counts reflect the box.
- [ ] **Field-name regression**: ARP/DHCP use `ip_address`/`mac_address` (not `ip`/`mac`); `hostname == "?"` → blank.
- [ ] **Scope safety**: with `scope_subnet_ids` set, stamping only hits IPs in those subnets (overlap-safe `.limit(1)`).
- [ ] **Rules / NAT viewer** (eye action) renders synced rules + NAT counts.
- [ ] **Graylog DSV** (Expose DSV on + a Graylog DSV token set): `GET /api/v1/lookup/pfsense/{id}/aliases?token=…`
  and `…/rules?token=…` return CSV/TSV; **wrong token → 401**; `expose_dsv` off → 404.
- [ ] Delete instance; periodic `jt-ipam-sync` picks up enabled instances every ~5 min without errors.

## 7b. VMware ESXi / vCenter integration (Admin → 整合 VMware) — **Beta**

> The SOAP endpoint is always `<url>/sdk`. One implementation covers **both** a standalone ESXi
> host and vCenter — they are the same VIM API, and ContainerView absorbs the depth difference.
> Use a **read-only** account: this integration never writes. Free/unlicensed ESXi exposes the
> API read-only anyway, which is exactly what is needed here.

- [ ] Add instance: URL + username/password, **Verify TLS off** for a self-signed cert; save
  (password write-only, never returned). Editing with an empty password leaves it unchanged.
- [ ] **Test connection** → step-by-step diagnostics: RetrieveServiceContent (product + version),
  Login, RetrievePropertiesEx (VM count). A wrong password must fail at **Login** with VMware's own
  message, not a bare "server error" — VMware returns auth failures as a SOAP Fault over HTTP 500.
- [ ] **Sync now** → VM count returns; clusters list shows the instance with type `vmware`;
  VMs carry name / power state / vCPU / memory / host.
- [ ] **Field reality check (first real hardware run)**: compare a few VMs against the vSphere client.
  Powered-off VMs have no `guest.*`, VMs without VMware Tools have no IP, templates have no
  `runtime.host` — none of these may break the sync; they should simply come back empty.
- [ ] **Paging**: on a vCenter with more than 200 VMs, the count matches the vSphere client
  (a dropped continuation token loses the rest **silently**).
- [ ] **IP matching**: an in-scope IP reported by VMware Tools links to the existing address;
  an address not in IPAM is **not** created. With overlapping subnets and no scope set, the
  ambiguous address is skipped rather than guessed.
- [ ] **Deleted VM**: remove a VM in vSphere → next sync removes it from the list.
- [ ] **PVE regression (shared tables)**: Proxmox clusters/VMs/interfaces are untouched by an ESXi
  sync, `legacy_vmid` and `kind=ct` still correct, and 進階 → 虛擬化 (Proxmox VE) still lists only PVE
  while 虛擬化 (VMware) lists only VMware. Device / IP links from PVE VMs still resolve.
- [ ] **Long external names (issue #25)**: a VM on an NSX-T portgroup whose name exceeds 64 chars
  syncs without error, and the full name (not a truncated one) shows on the VM's interface. The
  same goes for an ESXi host FQDN longer than 128 chars in `node`. A name coming from a third-party
  platform has no length we get to assume.
- [ ] Delete instance; periodic `jt-ipam-sync` picks up enabled instances every ~5 min without errors.

## 7c. Integration sync resilience — **applies to every integration, not just the one you changed**

Real devices are partially readable. A firewall answering "9 of 10 endpoints OK" is the
normal case, not an anomaly: firmware versions differ, and a read-only API account rarely
reaches every resource. What must never happen is one unreadable endpoint taking the rest
of the sync down with it (v0.5.195: an unreadable DHCP-lease path stopped ARP, policies,
NAT and address objects from syncing at all, while the UI showed a single error line).

- [ ] **Section isolation**: force one endpoint to fail (point it at a wrong path or revoke
  that one permission) and confirm every other section still syncs
- [ ] **Partial failure is visible**: the instance records what failed in `last_error`; a run
  with failures is never reported to the user as fully successful
- [ ] **No chain abort across instances**: one failing instance must not stop the sync round
  for the others (`session.rollback()` before writing `last_error`, or the next write explodes too)
- [ ] **Errors carry evidence**: a message like "response is not JSON" is useless in the field.
  Include status code, `content-type` and the first ~120 bytes, and name the likely cause
  (e.g. the device answered with its web UI, meaning that firmware lacks the endpoint or the
  API account cannot read it)
- [ ] **Connection test reflects reality**: the per-endpoint diagnostic shows the same result
  the sync would get — never a green tick for something the sync cannot read

## 7d. Probes run from a scan agent — **whenever the probe queue or the agent changes**

Letting the server hand work to an agent turns that agent into something that runs network
probes on request inside a customer network. The feature is only as safe as its narrowest check.

- [ ] **Kind allowlist**: anything outside ping / tcp / traceroute / rdns is refused — by the
  backend *and independently by the agent* (a compromised backend must not be able to widen it)
- [ ] **Target validation**: shell metacharacters, command substitution and argument injection
  (`-oProxyCommand=…`) are rejected; arguments are always passed as a list, never through a shell
- [ ] **Limits hold**: target count, port count, per-agent pending jobs, and clamped
  count/timeout values
- [ ] **Ownership**: an agent can only finish a job it claimed itself
- [ ] **Expiry**: with the agent stopped, a queued job expires instead of running late when the
  agent returns — a probe answering minutes after the question is worse than no answer
- [ ] **Round trip on a real agent**: create → claim → execute → report → read result, and the
  UI states which agent produced the output

## 7e. Audit chain anchoring — **whenever audit writes, anchoring or the sync schedule change**

What this section tests is the thing the chain itself cannot catch. Verifying the chain is not enough.

- [ ] **Tail truncation**: delete the last few rows after anchoring → must report
  `anchored_row_missing`; `verify_chain` alone reports "intact" for the same case, which is
  precisely why anchoring exists
- [ ] **Content tampering**: change the anchored row's hash → `anchored_hash_changed`
- [ ] **Shrinking count**: delete any middle row → `count_shrank` or `chain_broken`
- [ ] **Incremental**: the second verification resumes from the last anchor rather than
  rewalking the whole chain
- [ ] **Anchor file**: appended line by line (never rewritten), mode 0600, one corrupt line does
  not break reading; the same record also goes to journald (a copy survives file deletion)
- [ ] **Alerting**: on failure every admin gets a severity=error notification naming which case it was

## 7f. Zabbix integration — **whenever the Zabbix sync or coverage gap changes**

- [ ] **Three URL forms**: `https://host`, `https://host/zabbix` and a full `api_jsonrpc.php` all connect
- [ ] **Both auth modes**: API token and username/password each tested; the read response carries no secret
- [ ] **Stamps existing addresses only**: a host in Zabbix that IPAM does not know must not create an IP
- [ ] **Scope**: with `scope_subnet_ids` set, the same IP in an overlapping range is not stamped onto
  another tenant's address; queries use `limit(1)` (`scalar_one_or_none` aborts the whole round)
- [ ] **Hostname convergence**: two Zabbix hosts pointing at one IP must not overwrite each other every
  round (the change log must not fill up)
- [ ] **Coverage gap**: asking with a subnet scope answers only for those subnets; an empty scope
  returns empty rather than falling back to global

## 7g. Evidence contract — **whenever a source is added or changed**

What this section guards: **a new source must answer whether its evidence expires**.
The cost of not having that gate has already been paid — ARP was treated as timestamped
evidence, and a machine powered off for weeks showed 52 days of green.

- [ ] **Registered**: the new source declares its tier and `aging` in `services/evidence.py`;
  `pytest tests/test_evidence_contract.py` is green (the guard rejects unregistered sources)
- [ ] **Tier is right**: passively learned mappings (ARP/FDB/DNS/DHCP/virtualisation config)
  are `learned` with `aging=False`; only active probes and third-party monitoring may age
- [ ] **No string matching for source semantics**: no `"scanner" in status` style checks
  remain — ask `evidence.is_aging()`, so a new source cannot fall into the loosest branch
- [ ] **Liveness settings**: the options and defaults are derived from the contract;
  a non-expiring source is **not** selected by default
- [ ] **Availability bar**: days backed only by ARP are grey, not green; carrying a state
  forward requires the source that state claims to still exist
- [ ] **Precedence**: all five attributes (hostname/MAC/OS/device name/model) take effect
  immediately after a change and disabled sources really are excluded;
  `pytest -k "precedence or hostname or arp"` green
- [ ] ⚠️ **Cache**: precedence uses a module-level 60s cache cleared between tests by
  `conftest`'s `bust_all()`. If the cache moves, **verify that fixture still clears it** —
  it once failed silently and tests leaked settings into each other

## 7h. IP lifecycle and cooldown — **whenever release, allocation or the cooldown setting change**

- [ ] **Release starts a cooldown**: after deleting an address it appears under
  `/addresses/cooldowns/{subnet_id}` with the previous hostname and MAC
- [ ] **The record survives deletion** (releasing an address in practice means deleting it)
- [ ] **Allocation skips it**: neither the free-address list nor automatic allocation offers it
- [ ] **Manual creation is refused**: recreating the address returns 409 with a readable
  message including the end date and previous hostname — **not** `[object Object]`
- [ ] **Early clear**: after clearing, the address can be used again, but the record remains
  with who cleared it, when and why (recorded, not erased)
- [ ] **Disabled**: setting 0 days restores the previous behaviour and writes no record
- [ ] **Purge**: the sync round removes long-expired records but **keeps recently expired ones**
  (the days right after expiry are exactly when someone asks who had the address)

## 7i. Event rules — **whenever rules, conditions or event dispatch change**

- [ ] **Conditions are not expressions**: confirm nothing is evaluated; regular expressions
  are **unsupported** (ReDoS)
- [ ] **An unknown operator never passes** (passing is the dangerous default)
- [ ] **Field paths walk data only**: `data.x.y` must not reach attributes
- [ ] **AND semantics**: every condition must hold; no conditions means the event name decides
- [ ] **A broken rule does not stop the others**: a malformed rule is flagged and skipped while
  the remaining rules and the normal webhook dispatch continue (**never silently inert**)
- [ ] **Dry run has no side effects**: it reports what would match without sending anything
- [ ] **The webhook action uses the same path**: signing and the SSRF guard cannot be bypassed

## 7j. Topology access layer (FDB) — **whenever FDB inference or the topology map changes**

> FDB says "this MAC appeared on this switch port". Turning that into lines has two classic traps,
> and both of them draw a map that is confidently wrong rather than visibly empty.

- [ ] Access edges appear for hosts on ports carrying a single MAC, labelled with the port name.
- [ ] A port carrying more MACs than the threshold (an uplink/trunk) produces **no** access edges —
  the hosts beyond it are not drawn as plugged into that port.
- [ ] A port with several known hosts draws **dashed** edges (behind this port), not solid ones.
  Clicking such an edge shows "Directly attached: No" and the MAC count on the port.
- [ ] Two switches are joined only when each sees the other and the MAC sets behind the two ports
  are disjoint. In an A—B—C chain, **A—C must not appear**.
- [ ] A MAC that maps to more than one device (overlapping subnets) produces no edge at all.
- [ ] Unchecking 存取層 (FDB) removes every l2/l2_uplink edge; the rest of the map is unaffected.
- [ ] A department account that cannot see one end of a link does not receive that edge (no edge may
  reference a node that is not in the graph).
- [ ] **View modes**: the toolbar offers automatic / centred on switches / access layer only / subnets
  only. Automatic centres on switches when the range has FDB data and falls back to the subnet layout
  when it does not; "centred on switches" falls back the same way rather than drawing a centre-less
  layout.
- [ ] **Access layer (FDB) starts unticked**, and the default view therefore matches the pre-0.5.213
  subnet-centred picture.
- [ ] In the switch-centred layout the switches sit in the middle, their hosts above them, and each
  subnet node directly below its switch with subnet-only devices beneath it.
- [ ] **"Access layer only" hides devices with no FDB data** rather than scattering them as orphan
  dots (check on an estate where most devices have none).
- [ ] **Virtual machines (unticked by default)**: ticking it places each VM directly beneath its
  host inside the host's subnet box; unticking removes them entirely. A VM with no identifiable
  host, or whose node name matches several devices, is not drawn. A VM already mapped to a device
  does not appear twice.
- [ ] **Audit coverage**: `pytest tests/test_audit_coverage.py` is green. A new data-changing
  endpoint must either record an audit entry or be added to `EXEMPT` with a stated reason —
  never silenced just to make the test pass.
- [ ] **Rack diagram embedding**: after enabling it and generating a token in system settings,
  turn on one rack's toggle, copy the URL and open it in a **logged-out** browser — the image
  must render. A wrong or empty token returns 401; a rack that is not shared and one that does
  not exist return an **identical** 404; regenerating the token invalidates old URLs at once.

## 8. Recent feature spot-checks

- [ ] **Notification matrix** (Admin → 通知發送設定): toggle events × (in-app / email); save persists; events fire
  per matrix (IP request, cert expiring/deployed/drift, anomaly).
- [ ] **Cert distribution `files` profile**: writes cert files only, no reload/restart.
- [ ] **Anomaly page**: tabs, per-table column picker, `ip_address_id` hidden by default, MAC drift shows IP/hostname.
- [ ] **MCP client-config generator** (LLM/AI): button outputs Claude Desktop / opencode / mcpo / generic snippets.
- [ ] **LLM provider = OpenAI-compatible** (Admin → LLM/AI): switching to it shows the data-egress warning
  and the API-key field; the model dropdown repopulates from `/v1/models` (empty dropdown = the wrong path
  is being called); a base URL already ending in `/v1` is not doubled; chat and semantic search both work.
  Switching back to Ollama restores the `/api/tags` list. `select value from system_settings where key='llm'`
  must show **no plaintext key** — only `api_key_enc`; the settings page never returns the key itself.
- [ ] **Embedding dimension** (Admin → LLM/AI): the **Check dimension** button reports the model's actual
  dimension against the column size. After changing the embedding model, a reindex must report
  `failed: 0` — and if it reports `0 indexed` the failure count and reason must be visible, never a bare
  zero. A candidate model must also produce **different vectors for different Traditional Chinese
  descriptions** (English-only models collapse them and look fine while ranking at random).
- [ ] **Add address in a subnet**: the create form has a required IP field (issue #14).
- [ ] **Attach IPs by NIC MAC** (Admin → 系統設定): off by default on an existing install; **Preview**
  reports a count plus per-reason skips and changes nothing; enabling it attaches on the next sync round
  and writes one IP-change-log row per address with the match reason. Clear a device link by hand, then
  confirm the next round does **not** restore it (the rule that keeps the job from fighting the operator).

### Recent (v0.5.6x–0.5.7x)

- [ ] **BMC out-of-band console** (IPMI SOL, Beta): enable per IP (`bmc_enabled`, migration 0092) → connect
  button appears on IP detail + Connections; connects with cipher auto-fallback (17→3); credential vault
  “remember” persists (`protocol='bmc'`) and pre-fills next time; RBAC = same as SSH (per-object + can_ssh);
  session open/close audited; **Setup guide** modal opens (form/toolbar/blank-hint) with troubleshooting;
  **Fit to window** button sends `stty` (tooltip warns it sends a command).
- [ ] **Disconnected overlay** (SSH / RDP / VNC / noVNC / xterm / BMC): dropping the session shows a big
  centered “Disconnected” + broken-link icon **over the display only** (toolbar / Reconnect stay clickable);
  fades out on reconnect.
- [ ] **Connections OS column** matches the IP-detail page (shared `OsCell`): OS icon + localized family name
  + （source） annotation, raw guess on hover; value is the source-precedence-resolved OS.
- [ ] **Scan-agent OS detection** (agent ≥ 1.7.0): appliances/BMCs are no longer mis-guessed — Debian
  appliance (SSH banner) → `Debian`, Windows via SMB/Service-Info → `Windows`, device-model-only guesses
  (NAS / OpenWrt / router) are dropped to unknown rather than shown.
- [ ] **Notification i18n**: switch UI language (繁中 ⇄ English) → the bell **and** the Notifications page
  render in the current language (IP-request, anomaly, cert, stale-IP); old notifications fall back to stored text.
- [ ] **Notification channels** (Admin → 通知發送設定): Telegram / Slack / Teams / Nextcloud Talk / Zulip each
  save (encrypted token/webhook; “set — leave blank to keep”), the per-channel **Test** button delivers, and an
  enabled channel receives a matrix-fired event (e.g. an IP request) alongside Email/in-app.
- [ ] **Export button** on table pages is bordered (matches Columns / Refresh).
- [ ] **DHCP-server / gateway IP marking** (migration 0090 `is_dhcp_server`): OPNsense/pfSense DHCP-server IPs
  and gateways are flagged; IP detail shows the DHCP-server / gateway / in-DHCP-range badges.
- [ ] **LibreNMS auto-create device IPs** (migration 0091 default on): a LibreNMS-only device's primary IP is
  created in the matching (scoped) subnet; ambiguous overlaps are skipped, not mis-placed.
- [ ] **PVE browser console** (noVNC for VMs / xterm for CTs, migration 0089): per-IP toggle on PVE VM/CT IPs;
  connects with the PVE account; orange button + PVE badge on IP detail + Connections.

---

### Appendix: throwaway test DB commands (on the prod host, **never the prod DB**)

```bash
set -a; source /etc/jt-ipam/backend.env; set +a
sudo -u postgres psql -c "DROP DATABASE IF EXISTS jt_ipam_test;"
sudo -u postgres psql -c "CREATE DATABASE jt_ipam_test OWNER ${POSTGRES_USER} ENCODING UTF8 TEMPLATE template0;"
sudo -u postgres psql -d jt_ipam_test -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
cd /opt/jt-ipam/backend
POSTGRES_DB=jt_ipam_test .venv/bin/alembic upgrade head
JTIPAM_TEST_DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/jt_ipam_test" .venv/bin/pytest -q
sudo -u postgres psql -c "DROP DATABASE IF EXISTS jt_ipam_test;"
```
