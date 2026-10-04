# jt-ipam Release Test Checklist

> zh-TW version: [TEST_CHECKLIST_zh-TW.md](TEST_CHECKLIST_zh-TW.md).

> Rule: **before bumping the `version` in `frontend/package.json`, run this whole
> checklist once; only release when everything is green.**
> Treat it as the manual gate. Fix the red ones first; do not ship sick.

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

## 3c. Failures that come from the data, not the code: **whenever a read schema, an integration writer, or a recurring producer changes**

Three defects in one release (0.6.9) shared a shape the plan above does not catch: **the code is
correct, the data is legitimate, and the page still breaks, with nothing on screen saying why.**
None of them would have been found by "does the endpoint return 200 on a clean database".

### Read schemas must not be stricter than the database

A customer saw the dashboard count 55 devices while the device list returned Internal Server Error
and showed nothing. `DeviceRead` inherited the write-side limits (`vendor`/`model` ≤ 64 chars,
`u_position` 1–99) but those columns are `text` and an unconstrained `integer`, so an integration
wrote a value the database accepts and the read path rejects, and **one row killed the whole page**.

- [ ] For every read schema touched this release, compare each constrained field against the real
  column (`\d <table>`): a `max_length` on a `text` column, or a `ge`/`le` on an unconstrained
  `integer`, is a latent 500 waiting for an integration to write a longer value
- [ ] Constraints that the **database** enforces (CHECK, enum, varchar(n)) may stay strict on read:
  those values cannot exist
- [ ] Admin → System check → **data health** must report zero rows. It validates every row with the
  real read schemas, so "green here" means "the list pages can render this database"

> **Diagnostic shortcut worth remembering:** a count that works while the list 500s means the failure
> is *per-row serialization*, not the query; `count(*)` reads no columns, `select(Model)` reads all
> of them. The same asymmetry appears when the schema is behind (a missing column).

### An "ignore" must survive the next run of whatever produced the finding

AI audit findings came back after every run and had to be dismissed again and again, because the
identity was "category + the exact set of cited addresses" and the model cites a different subset
each time.

- [ ] For any dismiss / acknowledge / mute action: dismiss it, **run the producer again**, and
  confirm it does not reappear; pressing the button once is not a test of the button
- [ ] Confirm the opposite too: when genuinely **new** subjects appear, it *does* surface again.
  An "ignore" that also swallows new information is worse than one that does not stick
- [ ] Identity must not depend on model wording, ordering, or the exact subset cited

### If the system can detect it, the system must say it

An interrupted upgrade left the database behind the code. The backend could determine this at
startup; instead every page that reads full records returned 500 and the operator was left guessing.

- [ ] Any condition detectable at startup or during a request (schema drift, missing extension,
  unbuilt frontend, unreachable dependency) is **logged as an error and surfaced in the UI**, not
  left to be inferred from failures
- [ ] The message names the fix, not just the symptom
- [ ] Support questions are a test failure: if diagnosing an issue required asking the customer to
  run SQL or read logs, that diagnosis belongs in Admin → System check instead

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
- [ ] **Outbound guard** (`tests/test_safe_http_guard.py`, `tests/test_netdiag_http_guard.py`): `::ffff:127.0.0.1`,
  `::ffff:169.254.169.254` and `fd00:ec2::254` are refused; a DNS answer that changes to 127.0.0.1 after the check
  is refused at connect time (integrations, shared clients, Tools page HTTP check); a cross-host redirect drops
  credential headers, same-host and http to https keep them; Tools page TCP/UDP/TLS refuse loopback and
  link-local but still test private networks. After deploy: run each integration's Test connection once
  (TLS name check and HTTP/2 must still work through the connect-time guard).
- [ ] **Error text for ordinary accounts** (`tests/test_ai_error_codes.py`): with the LLM unreachable, a non-admin
  sees a translated "Cannot reach the LLM server" in AI chat and IP investigation, no host name; an admin sees the
  reason. A new error code needs `errors.<code>` in all three languages.
- [ ] **New code reported by GitHub code scanning**: check the open alerts after every push; fix or dismiss with a
  written reason (admin-only diagnostics are dismissed as "won't fix").

## 5b. Deploy-script flows (throwaway environment, **never run install on dev/prod**)

Every install problem customers have reported was invisible on an already-working
box, because there the thing is already there: a pre-existing PostgreSQL cluster
on a different major (so `pgvector` got installed for the wrong one), a `pnpm
install` that failed silently and left no frontend, an installer that printed
"Done" while nothing was running, and a backup unit whose `ReadWritePaths`
directory did not exist yet, which systemd reports as `226/NAMESPACE`, an error
that names nothing about the actual cause. **A clean-OS install is the only way
to see what a customer sees.**

- [ ] **Fresh install from a clean OS (required)**: `scripts/test-fresh-install.sh
  debian:12` exits 0. It starts a throwaway systemd container, copies the tree in,
  runs `scripts/jt-ipam.sh install`, then checks the things that only break in the
  field: the backend *answers* on its port, `jt-ipam-backup` and `jt-ipam-sync`
  actually run to `Result=success`, the backup unit survives its directory being
  deleted, and `doctor` agrees with reality
- [ ] Run it for **the oldest supported distro and the newest** (`debian:12`,
  `ubuntu:24.04`); PG-major and Node-version differences live there
- [ ] **Upgrade (required, and separate from the above)**: `scripts/test-upgrade.sh` exits 0.
  Fresh install and upgrade share almost no code, so passing the fresh-install gate says
  nothing about existing sites. Point it at the tree you are about to publish
  (`JT_IPAM_REPO=/path/to/candidate`), not at the last release; otherwise you are testing
  the version you already shipped. It writes a row *before* upgrading and checks it survived:
  losing data is the worst upgrade failure and it does not make any command exit non-zero
- [ ] Against a previous-version environment `scripts/jt-ipam.sh upgrade` also rolls back if needed
- [ ] If this release added a directory / package / service / DB extension / env,
  confirm **`install` and `upgrade` are both in sync**, and that `doctor` checks it
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
- [ ] **(C) Reference-data timers**: after a fresh install and after an upgrade, `systemctl list-timers` shows
  `jt-ipam-geoip-refresh`, `jt-ipam-oui-refresh` and `jt-ipam-recog-refresh`; after a fresh install the OUI
  table is not empty (it is fetched once during install); `doctor` lists all three
- [ ] **(C) Recog fingerprint database (optional)**: install / upgrade output shows "Recog: updated …
  fingerprints"; with outbound access blocked an upgrade only warns and still completes;
  `upgrade --recog-zip <recog-content-version.zip>` installs it offline; `python -m app.cli.recog status`
  shows the release
- [ ] **(D) Small machines** (`tests/test_resource_sizing.py`): a fresh install in a `--cpuset-cpus=0,1` or `--memory=4g`
  container runs 2 uvicorn workers (4 cores / 8 GB runs 4; `UVICORN_WORKERS` wins when set); an upgrade in a `--memory=3g`
  container without swap prints "pausing jt-ipam-backend during the frontend build" and the backend is up afterwards; a
  build forced to fail brings the backend back
- [ ] **(D) No zombies after an agent self-update** (`tests/test_agent_reap_inherited.py`): swap the server's agent.py while
  the agent runs an OS probe; within a round or two after the update `ps -eo stat,comm | grep -c '^Z'` is back to 0

## 5c. Real-browser testing: **mandatory for every release that touches the UI**

- [ ] Mobile sidebar (`frontend/e2e/mobile-sidebar.spec.ts`, 390×844): collapsed to zero width with the content
  starting at the left edge; the top-left button opens it over the content; picking a page or tapping the
  dimmed area closes it; desktop is unchanged
- [ ] Rack diagrams on phones (`frontend/e2e/mobile-rack.spec.ts`): pan left / right when wider than the
  screen; the Front / Rear toolbar stays inside the card
- [ ] **Every screen at phone width** (`frontend/e2e/mobile-all-routes.spec.ts`, 390px, routes parsed from the
  router): no page-level horizontal scroll, nothing clipped or off screen (unless a horizontally scrollable
  container holds it), no text squeezed to one character per line. With `E2E_SHOT_DIR` it screenshots every
  screen of every page. **Look at them**; the measurements cannot see "ugly but inside the screen"
- [ ] **Column resizing and tab scroll buttons** (site-wide; `e2e/anomaly-identify-cols-tabs.spec.ts`): dragging
  a header edge resizes that column by the distance dragged (hand-written tables too, including headers with
  opacity); **the layout before dragging is exactly as before** (a default minimum width once let tables
  without a fixed layout squeeze IPs into a vertical line on phones; run the phone sweep with it). Arrows
  appear only when the tab bar does not fit and only on the side that has more; the first and last tabs are
  reachable and the arrows never block a tab click
- [ ] The four phone reports (`frontend/e2e/mobile-overflow.spec.ts`): the sidebar scrolls under a finger and
  does not scroll the page behind; console status bars wrap instead of stacking one character per line; the
  notification popover stays on screen; rack diagrams default to a zoom that fits the phone and remember a
  change (separately from desktop). ⚠️ iOS's 100vh is taller than what is visible and Playwright cannot emulate
  the collapsing toolbar, so **have the sidebar fix confirmed on an iPhone**

Type checks, unit tests and API tests all pass while a page renders the wrong
thing, renders nothing, or puts it in the wrong place. Defects this project has
shipped that were only ever visible in a browser: a column added to a table but
not to the column-picker defaults (so it never appeared), an export that wrote
`undefined` into the report, a date overlapping its buttons, file names that
failed to line up by 16px, and a console that could not connect at all because
the reverse proxy dropped the WebSocket upgrade.

- [ ] `cd frontend && pnpm exec playwright test smoke` (no backend; self-starts
  vite preview) all green
- [ ] **Seed the fixtures first**: `POSTGRES_DB=jt_ipam_e2e python -m tests.seed_e2e`
  (from `backend/`). Several specs assert on specific records, and some of them
  *change* that data as they run -- dismissing an AI finding, for one -- so a second
  run without re-seeding fails on state left by the first. The failure looks exactly
  like a regression, which is how an hour gets spent on nothing
- [ ] Against a deployed instance (`E2E_BASE_URL` + `E2E_ADMIN_PASS`) run the
  **whole** suite: `pnpm test:e2e`. Data-dependent specs need real data, so run those
  against a deployed instance, not an empty test DB
- [ ] **A list page is not proved by opening it.** Compare what the page claims
  (the "共 N 筆" footer) against what the server reports, on a data set larger than
  one page: a page that fetches only the first page and then paginates in the
  browser looks perfectly healthy until someone has more records than that
  (GitHub issue #27: 95 sections, 50 shown, footer saying 50)
- [ ] **Every changed page opened in an actual browser**, console watched: no errors,
  no blank regions, no `undefined` / raw JSON / untranslated i18n keys on screen
- [ ] **A new spec covering what this release changed.** Assert on the effect, not on
  the UI's own claim: read the file back off the remote host, reload the page after
  saving, compare the downloaded bytes. "已上傳" on screen is not evidence
- [ ] **Measure geometry, don't eyeball it**: `boundingBox()` whenever the point is
  alignment, overlap or spacing; a screenshot hides a 16px error
- [ ] **Check narrow widths too.** A layout defect usually only exists below some width, so a
  test that runs at one wide viewport proves nothing: options spilled outside their card at
  820px while every existing test was green (reported by the user, v0.6.2). Any spec that
  asserts layout walks several widths (1500 / 1180 / 900 / 820 / 700), and the route sweep runs
  at 900px asserting no page scrolls horizontally
- [ ] New text checked in both locales (switch to English, confirm no key leaks)
- [ ] **Every route opens**: `playwright test e2e/all-routes.spec.ts` green. It parses the
  route list out of `src/router/index.ts`, so a new page is covered automatically, and it
  fails on blank screens, JS exceptions, failed API calls and untranslated keys. This exists
  because the sweep used to visit 22 of 78 routes: forty-odd pages had never been opened by
  any test. A page that only a human ever opens is a page nothing is checking

## 5g. Messages the server writes on the screen: **whenever an error message is added or changed**

The backend must not send finished sentences. Anything a person reads goes out as
`{code, params, message}` (`app/core/ui_error.py`) and the sentence is assembled from
`errors.<code>` in the browser. A Chinese sentence written into the backend shows up
*as Chinese* in the English and Japanese interfaces, and nothing errors; this shipped
undetected for the entire life of the English translation.

- [ ] `pytest tests/test_ui_error_codes.py`: every code in the source has a translation
  in all three locales (the test walks the source; a missing key is otherwise silent)
- [ ] New codes: read the fallback sentence in each locale and check the parameters are
  actually interpolated. A translation that drops `{reason}` **removes the diagnosis**:
  "pfSense returned an error" without saying whether it was DNS, a refusal or a certificate
- [ ] Switch the UI to English and Japanese and trigger at least one of the new errors for
  real. Codes used for *flow* (not just display) need extra care: the response interceptor
  flattens `detail` to a string, so read the code from `detail_code`; the Proxmox two-factor
  prompt was broken this exact way and nobody noticed
- [ ] No Chinese words passed as parameters (`what="下載"`): put the distinction in the code
  (`sftp_download_too_large`), or the English sentence ends up with a Chinese word in it

## 5h. guacd prebuilt binaries: **every release** (not only when the console changes)

We ship guacd ourselves, one build per operating system version: Debian and Ubuntu no
longer carry a usable package (Debian removed it, Ubuntu only has 1.3.0 with known RCEs).
The builds link against each distribution's own libraries, so **a new OS release needs a
new build**; a customer who upgrades to it otherwise loses the guacd engine. The owner's
instruction (2026-09-25): check online at every release and build for any new version.

- [ ] `scripts/guacd/check-new-os.sh`: asks endoflife.date which Debian (≥12) and Ubuntu
  (≥22.04, interim releases included while supported) versions are in support and compares
  them with `scripts/guacd/targets.txt`. Exit 1 lists what is missing: add it to
  `targets.txt`. Versions past end of support are listed as retirable
- [ ] guacamole-server upstream: new release or CVE since the pinned source in
  `scripts/guacd/source.env`? It is pinned to a `staging/1.6.1` commit because 1.6.0
  segfaults on Ubuntu 26.04 at the first frame; **once 1.6.1 is released, switch to the
  official Apache tarball and verify its published checksum**
- [ ] Walk the installer's guacd path on a clean OS too (guacd is the default RDP / VNC engine, so
  install puts it in by default): `scripts/test-fresh-install.sh debian:12` (the customer path:
  download from the GitHub release and verify) or `GUACD_TARBALL=<prebuilt for the same OS>
  scripts/test-fresh-install.sh debian:12` (a build not published yet); this checks that it installs,
  the service runs, answers on 127.0.0.1 **only**, and doctor is green. guacd is **required**:
  install must stop when it cannot be installed
- [ ] If anything changed: `scripts/guacd/build.sh` then `scripts/guacd/verify.sh` (both need
  docker; `APT_MIRROR` / `UBUNTU_MIRROR` as for the other gates). Verify installs only the
  runtime packages in a clean container **and connects to an RDP target for real**; the
  plugins loading is not enough (1.6.0 on 26.04 loaded fine and crashed on the first frame).
  Look at the screenshots it writes next to the builds
- [ ] configure must detect FreeRDP 3 correctly: the build fails on purpose if it reports
  "freerdp structs have a context... no" for a FreeRDP 3 target (the `-Wno-error` in
  CPPFLAGS is what prevents that; see the comment in `in-container-build.sh`)
- [ ] Every archive ships `LICENSE`, `NOTICE` and `SOURCE` (Apache-2.0 requires the first two;
  `SOURCE` points at the exact source and scripts). libvncclient is GPL-2+ and comes from the
  distribution; never bundle it. Never call the build an Apache release (ASF trademark)

## 5d. System export / import (cross-instance migration): **run in full every release that touches it**

- [ ] **Layout**: "Data to include" is one item per row, name and count on one line with the count right-aligned and the description below; nothing wraps badly at 1280 px or phone width and the list stays inside the card
- [ ] **Unit (no DB)**: `pytest tests/test_system_transfer.py -q`, covering crypto seal/open
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
- [ ] **Import streams** (`tests/test_system_transfer_streaming.py`): on the scale dataset export the default scope
  (27.5 MB, 427k rows) and run `import --dry-run` under `/usr/bin/time -v`; peak RSS stays around 200 MB (it was
  1.36 GB before 2026-10-01) and the per-table counts match; a wrong passphrase is reported as such and replace mode
  never wipes anything before the file has been verified; no decrypted content is written to disk

## 5e. AI chat / MCP tools: **every release that touches tools, prompts, or the data they read**

Wrong AI answers do not look wrong: every number in them is real, just computed over the
wrong set. Unit tests pass because each tool returns exactly what it was asked for; the
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
- [ ] **Identifiers from another table are mapped before a permission check**: `fdb_entries.device_id` /
  `arp_entries.device_id` are LibreNMS devices, not jt-ipam devices. Ask "which switch port is MAC …
  on?" as a department account that can see the switch: `trace_mac` must return the switch name and
  port (it compared the two kinds of id and never showed a port to anyone but an admin until
  2026-09-30); a switch the account cannot see stays hidden (`tests/test_mcp_rbac_scope.py`,
  `tests/test_rbac_gaps.py`)
- [ ] **Read-only stays read-only**: analysis/triage tools never write, never notify, never commit
- [ ] **Prompt injection**: attacker-controlled text (mDNS hostname, firewall rule description)
  stays fenced and truncated; the adversarial tests still pass
- [ ] **Facts come from tools, not arithmetic**: usage / free / count answers are fetched, never
  computed by the model from a CIDR

- [ ] **Cancellable**: while generating, Send becomes Stop; pressing it aborts the request
  (closing the connection also stops the LLM server), and the transcript says it stopped
- [ ] **Progress is visible**: connecting / thinking / which tool / composing / answering, each
  with the round number and elapsed seconds; **a bare spinner is indistinguishable from a hang**
- [ ] **An empty reply is never passed through**: the model is asked once more for a direct
  answer, and if it is still empty the reason is stated (length limit vs no text at all)
## 5f. Browser consoles (SSH / BMC / PVE): **whenever the terminal changes**

- [ ] **URLs are clickable**: when a TUI has broken a long URL across rows
  (`printf '%s\n' "$URL" | fold -w $(tput cols)` reproduces it), hovering the **second row**
  still recognises the whole URL, and the bar shows the full target
- [ ] **Only http/https open**, in a new tab with no opener (the text comes from the remote host)
- [ ] **Selection copy**: a URL split across rows copies as one usable address;
  **ordinary multi-line text must be left untouched**
- [ ] **No false joins**: a full-width line followed by unrelated text is not glued into a URL
- [ ] **SFTP sort mode**: with Folders first, directories lead in **both ascending and descending**
  order (putting the grouping inside the comparator inverts it on descending; that is the regression
  to watch); Mixed sorts purely by the column. Sorting by size or mtime honours the same mode.
  The choice is saved to user preferences and survives a reconnect or a different device
- [ ] **SFTP per-file limit (system setting)**: default 100 MB; a raised value sticks, and saving from an
  older page does not reset it; anything outside 1–102400 MB is refused with a message and reverted (it
  must **not** be clamped by the input and saved). Changing the limit runs the transfer path check
  **automatically**; the result gives upload/download speed and how long a file of the limit would take;
  a broken path names the kind (WebSocket blocked / 1009 message too big / cut mid-transfer / data not
  getting through). Existing spec: `e2e/sftp-limit-probe.spec.ts` (1009 simulated with routeWebSocket)
- [ ] **Large SFTP downloads**: over 64 MB, Chrome / Edge ask where to save and write to disk as data
  arrives (byte-identical, written in many pieces); other browsers fall back to memory (and refuse above
  2 GB with a message); over the limit is refused at once; progress shows while downloading.
  Existing spec: `e2e/sftp-stream-download.spec.ts` (needs `E2E_SFTP_ROOT`, `sftp-target.py` on 2223)
- [ ] **Run the path check on production too**, from outside through the edge reverse proxy; the dev
  machine's path does not have that layer
- [ ] Existing spec: `frontend/e2e/terminal-links.spec.ts` (needs `E2E_SSH_ADDRESS_ID/USER/PASS`,
  plus `can_ssh` on the user and `ssh_enabled` on the address; accept the host key on first use)

## 5e. Large-scale environments: **whenever sync, list pages, topology, exports or query patterns change**

The rule since GitHub issue #47 (a device with 30,000+ ports pushed an `IN` list past asyncpg's 32767
parameter limit): medium-sized test data cannot catch "one query fits" assumptions.

- [ ] Guard tests green: `tests/test_many_values_in.py` (no Python-list `IN` on sync paths, 30k values
  pass, batched writes), `tests/test_fk_indexes.py` (every foreign key has an index),
  `tests/test_topology_scale.py`, `tests/test_librenms_arp_sync.py` (an unchanged round costs a constant
  number of queries)
- [ ] Generate a large site: create `jt_ipam_scale` → `alembic upgrade head` →
  `POSTGRES_DB=jt_ipam_scale python -m tests.seed_scale` (2,000 /24s + a full /16, 145k IPs, 20k devices,
  200k ports with 40k on one device, 290k FDB, 100k leases, 500k IP changes)
- [ ] Point a backend at it and hit every GET: no 5xx, nothing taking more than a few seconds; the topology
  answers "too large" at 20k devices instead of freezing the backend
- [ ] Sync probe (fake API returning the same volume): a LibreNMS round finishes in minutes, and an
  unchanged round's query count does not grow with the number of rows
- [ ] Point the frontend at it: `e2e/all-routes.spec.ts` / `mobile-all-routes.spec.ts` green; open the /16
  subnet page and the 40k-port device page, and the longest main-thread block stays around 1.5 s or less
  (measure long tasks, do not eyeball)
- [ ] New lists, syncs and exports must answer: what happens at 100k IPs, tens of thousands of ports on one
  device, 100k leases (parameter limit, loading everything into memory, per-row queries, rendering
  everything at once, no pagination)
- [ ] **Run the GET sweep twice: as admin and as a broad non-admin account** (read on every section and on a
  location holding all devices). Admins skip visibility filtering, so the admin sweep alone never exercised
  it: an account that could see more than 32767 objects got a 500 from every list page and AI tool
  (2026-09-30). `tests/test_visibility_scale.py` pads the visible set to 40,000 ids; the `IN` guard in
  `tests/test_many_values_in.py` now scans all of `app/` and no longer trusts names containing "subnet"
- [ ] **Every integration's sync, not just LibreNMS** (fake upstream at the same volume, time + query count,
  an unchanged round must not grow with the rows): Wazuh 30k agents (`tests/test_wazuh_scale.py`, including
  SCA: at most 200 agents per round, oldest first, stops on HTTP 429), OCS (`tests/test_ocs_scale.py`),
  Zabbix, ESXi, DNS, AdGuard (`test_*_scale.py`), the five firewalls and Windows / Kea / ISC DHCP through
  `services/fw_sightings.py` (`tests/test_fw_sightings.py`). A duplicate key in one upstream response must
  not fail the sync
- [ ] **The site-wide liveness recompute** runs every 5 minutes over every IP: over ~6,500 IPs it used to
  exceed the parameter limit and never update statuses again (`tests/test_liveness_scale.py`, 8,000 IPs).
  On the scale DB an unchanged round is ~10 s and 3 queries
- [ ] **Background jobs on the scale DB** (anomaly detection, system diagnostics, pool usage, pruning,
  audit chain): each round completes in seconds; the audit chain is verified in batches of 5,000
  (`tests/test_audit_anchor.py`) so a first verification over millions of rows does not load them all
- [ ] **System export / import memory**: export the scale DB with `/usr/bin/time -f %M`; default scope
  stays around 150 MB RSS and the full scope around 350 MB (it was 1.7 GB / 5.4 GB before streaming); the
  file decodes to the same content (`tests/test_system_transfer.py::test_streamed_export_is_the_same_file_format`).
  Import into a fresh DB writes in batches of 1,000 with a per-row fallback (`::test_batched_import_isolates_a_bad_row`);
  note the import still parses the whole file in memory, so size the target host accordingly

## 6. Manual page review (browser, after deploy)

- [ ] Login / logout / theme switch (light / dark / auto)
- [ ] Subnets: list, tree, IP list (incl. idle-range rows spanning columns), edit
- [ ] Devices / racks: sorting (natural IP order), consistent action-button height,
  floor-plan upload + drag-to-place + select
- [ ] **Dashboard racks card** (bottom, `e2e/dashboard-racks.spec.ts`): unconfigured shows "Settings"; pick a room → its
  racks drawn in one row standing on the same floor (no border in the dark theme), names open the Racks page; switch to
  selected racks → only those; the setting survives a reload and another browser; more than 12 shows "N more" linking to
  the Racks page
- [ ] **Dashboard card headers hold only the title (at most a count)**: no buttons, no subtitle text; the racks card's
  Settings button and scope sit at the top of its body
- [ ] **IP details "Last seen by source"** (`e2e/ip-seen-sources.spec.ts`): a section of its own with source / time / ago
  columns in a fixed order (scanner, LibreNMS, ARP, Wazuh, OCS, each firewall, AdGuard), the most recent row bold and
  tagged "Latest"; the basic fields no longer carry last-seen rows; on a phone "ago" moves under the time with no
  horizontal scroll. Clicking the LibreNMS / Wazuh / OCS time opens the device page scrolled to that card (outline flashes)
- [ ] **Last seen by source: verdict and sorting** (`e2e/ip-seen-sources.spec.ts`, `tests/test_ip_seen_rule.py`): the scanner
  within the limit reads "Counts · current", an old LibreNMS time "Counts · expired", DHCP lease and AdGuard "Not counted"
  (and follow the liveness sources in system settings); one click on Time puts the newest first; phones show three columns
  without horizontal scrolling; the IP list API carries no `liveness_rule`
- [ ] **SFTP name clashes** (`tests/test_sftp_upload_conflict.py`, `e2e/sftp.spec.ts`): uploading an existing name asks
  Overwrite / Keep both / Skip; Keep both creates "name (1).ext" and leaves the original; Overwrite replaces the content and
  keeps the permissions; with several clashes "do the same for the rest" asks once; cutting the link half way through an
  overwrite leaves the original intact and no `.jtipam-upload-` leftovers; overwriting works on SFTP servers without
  posix-rename
- [ ] **Device type column on the subnet page's IP list**: the column picker offers it and it looks the same as on the
  Addresses page (icon and name, model on hover)
- [ ] **Device type vendor and ambiguous guesses** (`tests/test_device_identity.py`, `tests/test_recog.py`): a SuperMicro
  machine is no longer labelled HP (jt-ipam's OUI table wins); Linux vs HP P2000 within a point or two comes out as server;
  port 8006 open means hypervisor
- [ ] **SFTP rate** (`e2e/sftp.spec.ts`): large uploads and downloads show "MB/s · about … left" after the progress; with the
  link cut or throttled to almost nothing it turns into "0 B/s · stalled" within seconds
- [ ] **GraphQL removed**: `POST /graphql` is no longer GraphQL (404, or 405 from the static frontend); the version page's
  package list has no strawberry
- [ ] **Agent OS probe with a slow service** (`tests/test_device_identity.py`): a periodic OS probe of a host with PVE's 8006
  open ends up with a device type (it used to time out with nothing)
- [ ] **Virtual/physical shows the guest kind** (`tests/test_virt_correlation.py`): a PVE LXC reads "Container · LXC",
  qemu "Virtual machine · KVM", VMware "Virtual machine · VMware", the same on IP details and the device page
- [ ] **Counts in subnet page card headers**: "Address ranges (pools)" and "IP list" show the count as a tag next to the
  title, not in brackets
- [ ] Topology: nodes / links, VPN pairing links, legend
- [ ] **MAC history** (`tests/test_mac_history.py`, `e2e/mac-history.spec.ts`): typing a full MAC into global search (upper
  case, dashes, Cisco dots all work) shows "Full history of …" on top, Enter goes straight there and clears the box; the MAC
  in IP details, old/new MAC values in the change history and MACs in anomaly detection all link there. The page: IPs used
  (in-use first, first and last seen, evidence, status light; IPs whose record was deleted still listed and marked),
  switch ports (LibreNMS and MikroTik), timeline (took / left, previous and next MAC), DHCP reservations, device ports and
  VM interfaces. Random MACs carry a tag and an explanation; "probably the same device" only lists handovers where the
  host name stayed the same. A department account sees only IPs in granted subnets and granted devices, and no DHCP or
  VMs (the page says so). A non-MAC returns `mac_invalid`. On production, check a MAC that really moved against the IP
  details change history.
- [ ] **Console relay through scan agents** (issue #24 phase 2; `tests/test_console_relay.py`, `e2e/console-relay.spec.ts`).
  Setup: two containers as customer sites, each with a dummy interface 10.99.0.5/24 (overlapping), sshd, a marker file and a
  scan agent dialing back to the backend (no relay variables set on the agent host); two 10.99.0.0/24 subnets assigned to their own agent as
  scan agent and console exit; the backend host cannot reach 10.99.0.5. Check: SFTP to each IP lists its own marker file and the
  status bar says "via scan agent: <name>"; an SSH session works the same way; a second worker process gets the port over Redis.
  Everything is set in the web UI: with the system switch and "Allow console relay" on the agent page turned on, the agent
  host needs nothing and consoles connect right away (no waiting for the next poll); setting Allowed ports to 2222 lets SSH
  on 2222 through and refuses 22 (`relay_port_not_allowed`); an invalid list (`abc`, `1-70000`) returns
  `relay_ports_invalid`. Refusals, never direct: system switch off, agent not allowed (the poll hands out an empty scope and
  the agent relays nothing), agent too old, the host owner refusing with `JT_IPAM_RELAY=0` (`relay_agent_host_off`; the
  agent also refuses when the server is forged to think it is on), target outside the agent's subnets, port not allowed,
  agent offline (`relay_agent_timeout` after 15 s). `JT_IPAM_RELAY_PORTS` / `_MAX` / `_CIDRS` on the agent host can only
  narrow, and the agent page's status tag lists those local limits. Restarting the agent and relaying right away works (no lost job).
  Audit has `console_relay` with bytes each way. Subnet edit shows the subnet's scan agent as a console exit (greyed out with
  the reason when it cannot relay); changing the scan agent while the exit points at the old one is refused. Upgrade adds the
  nginx location (`grep scan-agents/relay /etc/nginx/sites-available/jt-ipam`, `nginx -t`).
- [ ] **Disabled jump host refuses** (`jump_host_disabled`) instead of connecting directly; IP edit saves a per-IP jump host or agent.
- [ ] **Create IPs from the LibreNMS ARP table** (#48, `tests/test_librenms_arp_autocreate.py`): LibreNMS integration →
  edit → "Create IPs from the ARP table" is off on a new and on an upgraded site. Turn it on → the warning (no longer
  under Unauthorized IPs, not liveness evidence) and two options appear: "Require a switch MAC table sighting" (on) and
  "Skip DHCP dynamic ranges" (on); unticking the first shows the ARP-only warning. Sync → new addresses have source
  "LibreNMS ARP", the auto-collected tag, a MAC and a "created" entry in the change history; the Tasks summary shows
  "IPs created from ARP N" and the top reasons for the rest. A device unplugged hours ago that still sits in the router's
  ARP cache is NOT created while the MAC-table option is on. Overlapping subnets without a scope, proxy ARP, broadcast
  and network addresses, released (cooldown) addresses and DHCP pools are skipped. The created IPs do not turn online on
  ARP alone. The audit log entry for the settings change lists the changed fields.
- [ ] **Site-to-site VPN from every vendor** (`tests/test_vpn_site_to_site.py`): the default "Subnets only" view has VPN
  lines; FortiGate IPsec tunnels, Palo Alto IPsec tunnels ("Site-to-site VPN" in the integration settings, on by
  default; Test connection shows `vpn_flow`) and MikroTik WireGuard all know their own device (left side of
  "Pairing / peer" on the site-to-site VPN page). With both ends in jt-ipam they become one line (public keys for
  WireGuard, endpoint addresses for IPsec, across vendors too); an unknown far end is drawn as a remote site.
  ⚠️ Palo Alto has not been checked against a real device: on a customer site compare the `vpn_flow` row count and
  tunnel states.
- [ ] Scan agents / sync jobs: pages render, no console errors
- [ ] **Docs site (GitHub Pages)**, on every release that adds a feature or an integration: the feature map
  (`docs/features.html`) and the home page integration badges name it (each product, not a category such as
  "DNS"); every feature-map item fits on one line at desktop width in zh / en / ja; every section heading has a
  `#` link with an English anchor and `page.html#anchor` opens scrolled to it (API manual subsections too);
  `git ls-files '*.md' '*.html' | xargs grep -lP '\x{FF0F}'` finds nothing (docs use a half-width slash)

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

## 7b. VMware ESXi / vCenter integration (Admin → 整合 VMware): **Beta**

> The SOAP endpoint is always `<url>/sdk`. One implementation covers **both** a standalone ESXi
> host and vCenter: they are the same VIM API, and ContainerView absorbs the depth difference.
> Use a **read-only** account: this integration never writes. Free/unlicensed ESXi exposes the
> API read-only anyway, which is exactly what is needed here.

- [ ] Add instance: URL + username/password, **Verify TLS off** for a self-signed cert; save
  (password write-only, never returned). Editing with an empty password leaves it unchanged.
- [ ] **Test connection** → step-by-step diagnostics: RetrieveServiceContent (product + version),
  Login, RetrievePropertiesEx (VM count). A wrong password must fail at **Login** with VMware's own
  message, not a bare "server error"; VMware returns auth failures as a SOAP Fault over HTTP 500.
- [ ] **Sync now** → VM count returns; clusters list shows the instance with type `vmware`;
  VMs carry name / power state / vCPU / memory / host.
- [ ] **Field reality check (first real hardware run)**: compare a few VMs against the vSphere client.
  Powered-off VMs have no `guest.*`, VMs without VMware Tools have no IP, templates have no
  `runtime.host`. None of these may break the sync; they should simply come back empty.
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

## 7b2. MikroTik RouterOS integration (Admin → 整合 MikroTik): **Beta**

> **The point of this integration is not to slow the router down.** At the site that asked for it,
> the MikroTik boxes are the *main* routers, so the safeguards are the feature; test them, not
> just the field parsing. RouterOS needs `www-ssl` enabled and an account with `api` + `read`.

- [ ] Add router: URL + username/password, **Verify TLS off** for a self-signed cert; save
  (password write-only, never returned). Editing with an empty password leaves it unchanged.
- [ ] **Test connection** reports RouterOS version, board name, identity, CPU before/after, and
  **rows + seconds for every endpoint**. Numbers are the whole point: "ARP 12,000 rows / 3.2s" is
  how an administrator decides whether to enable that section.
- [ ] A menu the device does not have (`/ip/dhcp-server` on a switch, `/interface/wireguard` on
  RouterOS 7.0) shows as **absent, not an error**; a switch must not finish a sync covered in red.
- [ ] **RouterOS 6.x is named**: pointing at a v6 device must say "this is RouterOS 6.x, which has
  no REST API", not a vague connection failure.
- [ ] **Sequential, never parallel**: watch the router's own connection/CPU graph during a sync;
  only one request should be in flight at a time. (Packet capture, or RouterOS `/tool/profile`.)
- [ ] **Back-off works**: lower the CPU threshold to something the router already exceeds (e.g. 1%)
  and sync → the round stops early, the list shows the「提早停止」tag with a reason, and
  `last_error` stays **empty** (stopping early is not a failure).
- [ ] **Size cap**: set the response cap to 1 MiB on a router with a large address list → that
  section aborts with a readable message naming the limit, and **the other sections still run**.
- [ ] **ARP is reachable-only**: an entry the router shows as `stale` or `permanent` must not stamp
  the IP as online. (Check `arp_seen` on the IP: only `reachable` entries may appear.)
- [ ] **DHCP three-table join**: ranges only appear when pool ↔ dhcp-server ↔ network line up; a
  pool used by PPP/hotspot (no DHCP server pointing at it) must **not** show up as a DHCP range.
  A subnet that already has a gateway or DNS set is **not** overwritten.
- [ ] **Rule order is preserved**: the read-only view lists rules in the router's own order
  (RouterOS matches top-down). Moving a rule in Winbox must **not** raise a rule-change alert;
  editing one must.
- [ ] Delete the router → its `dhcp_pool_ranges` and `nat_translations` rows go with it, and
  no other source's rows are touched; its FDB and neighbors go too, and the ports it created are
  released (uncabled ones deleted, cabled ones kept as manual ports).

**Phase 2: interfaces, neighbors, FDB (migration 0170, `tests/test_mikrotik_phase2.py`)**
- [ ] **Device mapping**: the settings "Device" field searches by name on the server (found even with
  tens of thousands of devices); left empty, a sync fills in the device that owns the IP of the API
  address. An unknown device returns `422 ros_device_not_found`.
- [ ] **No device is not a failure**: an API address that matches no device-linked IP → the
  interface / neighbor / FDB sections are skipped, the list shows a「待指定裝置」(Device needed) tag
  next to Last sync (reason on hover), and `last_error` stays **empty**.
- [ ] **Interfaces → ports**: the device's ports show the physical ports (ether / sfp / wlan…) with
  MAC and comment; virtual interfaces (bridge / vlan / pppoe / wg) must **not** appear. Remove an
  interface on the router → that port is deleted next round; cabled and hand-made ports are untouched.
- [ ] **Neighbors**: Advanced → 路由器 (MikroTik), "Neighbors" tab, lists local port, neighbor name, remote port,
  IP, MAC, platform and discovery protocol; a name links to the device when the announced address or
  MAC matches **exactly one** device (overlapping subnets: no guess). A neighbor seen on both the
  physical port and the bridge is listed once (the physical port).
- [ ] **Topology**: neighbors become backbone links between the two devices (label "local ↔ remote",
  via "Neighbor discovery (MikroTik)", evidence "monitored"); with FDB on, hosts on router ports are
  drawn too.
- [ ] **FDB**: off by default (a large bridge can be tens of thousands of rows; check the row count in
  Test connection first). When on, the router's own MACs (`local=true`) and rows on the bridge
  interface itself are skipped. **On a site with MikroTik and no LibreNMS**, IPs still get a switch
  port ("device / port", same rule as LibreNMS).
- [ ] **Every FDB reader names the MikroTik switch**: AI chat "where is this MAC" / "which port is this
  IP on", VLAN members, and `/api/v1/librenms/fdb` (`source=mikrotik`, `switch_device_id`) show the
  router's device name, not "?" or blank.

## 7l. Console jump host (issue #24 phase 1): **whenever a console or the routing changes**

> The failure mode here is not "cannot connect", it is **"connected to someone else"**. Sites that
> need a jump host are usually the sites with overlapping private ranges, so a console that quietly
> falls back to a direct connection reaches a *different customer's* machine, with no error anywhere.
> Test every console, not just SSH.

**Standing up a real jump host takes two minutes** (do not skip this and test only the unit level):

```bash
D=/tmp/jump; mkdir -p $D && cd $D
ssh-keygen -q -t ed25519 -f hostkey -N ''
ssh-keygen -q -t ed25519 -f clientkey -N ''
cp clientkey.pub authorized_keys
printf 'Port 2242\nListenAddress 127.0.0.1\nHostKey %s/hostkey\nPidFile %s/sshd.pid\n' $D $D > sshd_config
printf 'AuthorizedKeysFile %s/authorized_keys\nPermitRootLogin prohibit-password\n' $D >> sshd_config
printf 'PasswordAuthentication no\nUsePAM no\nStrictModes no\nAllowTcpForwarding yes\n' >> sshd_config
printf 'Subsystem sftp /usr/lib/openssh/sftp-server\n' >> sshd_config
/usr/sbin/sshd -f $D/sshd_config -E $D/sshd.log
```

`StrictModes no` is needed because sshd rejects an `authorized_keys` under a world-writable `/tmp`.
The same sshd can play both roles: register it as the jump host, and point the target IP record at
`127.0.0.1` port 2242 so the forward lands back on it.

- [ ] **Fingerprint first**: a jump host with no pinned host key must **refuse to connect** and say so.
  Test connection returns the fingerprint *without* sending credentials; only after Trust and save
  does it actually log in.
- [ ] **Wrong fingerprint**: change the pinned value → connecting must fail with a man-in-the-middle
  warning, not a generic error.
- [ ] **Resolution order**: set a jump host on the subnet and a *different* one on the IP → the IP
  wins. Disable the jump host → the console **refuses** with "jump host is disabled" (since 2026-10-02:
  falling back to direct on overlapping networks reaches the wrong host); removing the assignment
  is the way to connect directly.
- [ ] **All four tunnelled consoles** (SSH / SFTP / RDP / VNC), each through the jump:
  - SSH: the status bar shows「經由跳板：<name>」and a real shell responds
  - SFTP: a directory listing appears (this proves both directions, not just server→browser)
  - RDP / VNC: check the **port**, not just the host; aardwolf's `create_connection_newtarget()`
    replaces the ip/hostname but keeps the port from the URL, so a missing port means connecting to
    `127.0.0.1:3389`, the backend host itself
- [ ] **BMC refuses**: an address with a jump host must return a readable "IPMI is UDP, an SSH
  tunnel only forwards TCP" error, **never** a silent direct connection
- [ ] **Connection reuse and limit**: open several sessions to the same jump host → one SSH
  connection is shared (check with `ss -tnp` on the jump); exceeding `max_sessions` is refused with
  a readable message; after the last session closes the connection goes away
- [ ] **Failure returns the reference**: force a forward failure a few times (wrong target port),
  then confirm normal sessions still work; a leaked reference count silently uses up the limit
- [ ] **Session lifetime**: close the browser tab → the forward disappears from the jump host
- [ ] **Deleting a jump host** warns how many subnets/addresses will fall back to direct
- [ ] **Requirements guide** (`e2e/jump-hosts.spec.ts`): both the Requirements button and "What does a jump host need?" in
  the create dialog open it; it covers system, network, forwarding, account (no root or shell needed), authentication
  (passphrase-protected keys not supported) and host key; following the example on a clean Debian/Ubuntu (OpenSSH) host,
  an SSH console through it works, and an interactive `ssh -tt` with that key is refused (PTY allocation request failed)
- [ ] Audit records `via_jump_host` on every session open

## 7m. guacd console engine: **whenever a console, guacd or its build changes**

guacd is the default engine for RDP and VNC (since 2026-09-27; migration 0158 switches existing
installs), and SSH can use it (Admin → System settings, per protocol). Switching must not change
what a console is allowed to do.

- [ ] Defaults: on a fresh install the settings page shows "guacd (default)" for RDP and VNC and
  "Built-in (default)" for SSH; after upgrading an old site RDP / VNC are guacd
  (`frontend/e2e/rdp-engine.spec.ts`, `tests/test_console_engine_default.py`)
- [ ] guacd is required: Version info → Required components lists it (version, running); aardwolf
  is under Optional
- [ ] The security section of System settings is one setting per row: name and explanation on the left, the control on
  the right, a divider between rows; missing packages, guacd not running and the transfer path result span the full
  row under that setting; at phone width the row stacks
- [ ] Overlays sit above the AI assistant button: a confirmation / dropdown that opens in the bottom-right
  corner can be clicked where it overlaps the button (`frontend/e2e/chat-fab-overlays.spec.ts`)
- [ ] The Required components card stays readable with a long guacd version string (`… for Ubuntu
  24.04 LTS (amd64)`): the name column is not squeezed, the status is on the right, the version (without
  the OS suffix) is under the name, at desktop and phone widths (`frontend/e2e/version-required-deps.spec.ts`)
- [ ] With guacd stopped, RDP / VNC **still connect** (built-in engine fallback, if the optional
  aardwolf is present), the settings page shows guacd red and doctor / System check fail; a session does not hang when guacd goes up or down in
  the middle (the engine travels in the ticket and the WebSocket follows it)

- [ ] `frontend/e2e/console-guacd.spec.ts` against a local guacd and the three targets (see the file
  header: xrdp container on 3389, `e2e/fixtures/vnc-target.py` on 5999, an sshd on 2222). It checks
  that the screen is really painted (pixels, not just a canvas), that keys and Chinese leave as
  Guacamole `key` instructions, and that Ctrl+Shift+V sends the clipboard **before** the V
- [ ] Look at the screen yourself once per protocol (the test cannot read text):
  RDP types, VNC shows the target, SSH shows the prompt and **Chinese is full width** (a narrow,
  tiny glyph means guacd is not running under a UTF-8 locale; the unit sets `LANG=C.UTF-8`)
- [ ] SSH: an already pinned host key must be accepted by guacd (our patch
  `scripts/guacd/patches/0001` makes libssh2 negotiate the pinned key type); a changed key must
  still fail with the "host key does not match" message
- [ ] Credentials never reach the browser: the WebSocket carries the config message, then only
  Guacamole instructions; the server drops anything but key/mouse/size/clipboard/sync/nop/…
  (`tests/test_guacd.py::test_relay_forwards_allowed_and_drops_the_rest`)
- [ ] Clipboard policy is unchanged by the engine: RDP paste only when "RDP clipboard paste" is on,
  nothing back from the remote; VNC none; SSH copy and paste
- [ ] A background tab stays connected for more than 5 minutes (browsers throttle timers to once a
  minute there; the server sends the keep-alive, not the page)
- [ ] `sudo jt-ipam.sh doctor` and Admin → System check show guacd; stopping `jt-ipam-guacd` must turn
  both red with the fix and say the built-in engine is in use; when the built-in
  engine is unavailable too (no aardwolf, say), a ticket request must answer with a readable 503
- [ ] VNC username: on a server that asks for one (the VeNCrypt target on 5998 in the spec header)
  an empty username must say "enter the username", a filled one must connect; a wrong password must
  say "wrong username or password", not "host unreachable"; unreachable only when it really is (TCP
  is probed only **after** guacd failed: TigerVNC counts a bare connect/close as an authentication
  failure and blocks the source after a few; if everything fails halfway, look for `blacklisted` in
  the target's log)
- [ ] The status bar names the engine of this connection ("Engine: guacd" …); RDP / VNC carry no Beta mark
- [ ] **High-DPI screens**: on a Retina / 200% display the remote screen is sized in device pixels (sharp, not
  blurred) and SSH text is not twice as large; **A- / A+ change the SSH font size during a guacd session** and
  the size is remembered (only the font size reaches guacd, validated)
- [ ] Known limitation to keep in mind: in the SSH terminal, the first Chinese character typed on a
  line may not be drawn until the line is redrawn (Ctrl+L); the command itself is correct

## 7b3. Standalone Kea / ISC DHCP servers (issue #45): **whenever these integrations, the agent's dhcpd report or the shared DHCP write layer change**

- [ ] **A real Kea round trip** (a throwaway container is enough: Ubuntu 24.04 packages Kea 2.4 behind the Control
  Agent; ISC's own repository has Kea 3.0 for the direct socket): test connection returns the version and the mode
  (Control Agent / direct); a sync writes pools (range and CIDR forms, subnets under shared networks), reservations
  and leases (the existing IP is marked leased, MAC source kea_dhcp, host name); works with and without host_cmds;
  without lease_cmds pools still sync and the page says so; a wrong password fails with the 401 reason.
  ⚠️ The backend's outbound guard blocks loopback: bind Kea on the docker bridge (172.17.0.1) and enable
  OUTBOUND_ALLOW_PRIVATE on the local backend
- [ ] **A real isc-dhcp-server round trip**: the distribution's default dhcpd.conf (full of commented-out examples)
  yields nothing; `include` files are followed; the secret in a `key` block never appears in a report; after a
  client takes a lease the agent reads the real dhcpd.leases → fixed addresses are marked reserved, leases leased;
  a later record for the same address overrides an earlier one
- [ ] The agent reads the files only when the server assigns it an ISC source (`dhcpd` in the poll response);
  another agent cannot report for a source that is not its own (404); one agent serves one source
- [ ] An unreadable file (permissions, wrong path) keeps what was there and the last error names the file and the
  reason; an agent silent for 3× its report interval marks the source as failing (health alert)
- [ ] Deleting a source takes back its pools / reservations / leases / host names from the shared tables
- [ ] `e2e/dhcp-standalone.spec.ts`: a failing Kea test connection shows the real reason (not the browser's own
  15-second timeout); ISC file status; an agent already in use is disabled in the picker

- [ ] **Device import (issue #46, `e2e/device-import.spec.ts`, `tests/test_device_import.py`)**: a file exported from
  the list (once each in the zh / en / ja interface) imports back unchanged; the template with current devices imports
  back in update mode with zero errors; location / rack / unit by name, a rack alone implies its location, a rack
  name used in two locations asks for the location; existing devices are skipped / updated (blank never clears);
  rack-position overlaps within one file are refused; rows with errors are not written at all; the preview leaves
  nothing behind; every device is audited; .xlsx works; a value starting with = gets a leading quote in the template
  and loses it on the way back in

## 7c. Integration sync resilience: **applies to every integration, not just the one you changed**

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
  the sync would get, never a green tick for something the sync cannot read
- [ ] **What the upstream deleted must disappear** (2026-09-26 audit: hostnames from 16 sources and DNS
  records were never removed): delete one item upstream (DNS record, lease, VM, agent, host) and after
  one sync the IP's hostname and mirror rows must be gone. Hostnames always go through `HostnameRun`
  (`services/hostname_reports.py`): `report` what you see, `hold` entities whose data is uncertain this
  run, `finish(complete=…)` at the end; **complete must mean "this run really read everything"**, never
  a copy of the heartbeat's ok
- [ ] **Unreadable must not mean removed** (the opposite defect): make an endpoint time out / return 403
  for one run; existing hostnames, NAT, policies, VPN tunnels and DHCP ranges / reservations must stay
  untouched and `last_error` must say why. Only 404 (the feature does not exist there) counts as "read,
  nothing there". A VDOM / vsys list that fell back to a default is not a complete list; sections that
  replace a whole snapshot must not run
- [ ] **Instances of the same kind do not remove each other's data**: two firewalls of one vendor each
  report their own; when one stops, what the other still reports stays
- [ ] **Breaker**: make the API return an empty list for one run (permission revoked); hostnames must
  not be wiped, `last_error` must say why, and the rule-change sentinel must not report "all removed"
- [ ] **An unchanged field is not a manual edit**: change only the description in the IP edit form and
  save; neither the hostname source nor the MAC source may become manual
- [ ] **Device ports follow LibreNMS** (2026-09-27: a pulled dual-port NIC and USB NICs stayed in the
  list although LibreNMS had marked them deleted): pull a NIC / unplug a USB NIC, let LibreNMS rediscover,
  then sync or press "Import from source", and its ports disappear from Ports / cabling; ports you created
  yourself, cabled ports and pass-through-mapped ports stay; a failed read or an empty port list removes
  nothing. Docker `veth…` interfaces are never imported (`tests/test_device_ports_reconcile.py`)
- [ ] **Cannot connect = failed, never "succeeded, 0 records"** (#44): point an integration at an unreachable
  host and at a wrong token; the task ends as failed with the last error (Proxmox with every node failing,
  LibreNMS, AdGuard…) (`tests/test_sync_total_failure_is_failure.py`)
- [ ] **The same key twice in one response** (#43): an upstream that repeats a row (the same MAC / port / VLAN)
  must be merged in memory, not hit a unique constraint; and a task whose database session broke must still
  end as "failed" with the error, **never stay "running"** (the final status is written with a clean session)
  (`tests/test_librenms_fdb_duplicates.py`)

## 7d. Probes run from a scan agent: **whenever the probe queue or the agent changes**

Letting the server hand work to an agent turns that agent into something that runs network
probes on request inside a customer network. The feature is only as safe as its narrowest check.

- [ ] **Kind allowlist**: anything outside ping / tcp / traceroute / rdns / identify is refused, by the
  backend *and independently by the agent* (a compromised backend must not be able to widen it)
- [ ] **Target validation**: shell metacharacters, command substitution and argument injection
  (`-oProxyCommand=…`) are rejected; arguments are always passed as a list, never through a shell
- [ ] **Limits hold**: target count, port count, per-agent pending jobs, and clamped
  count/timeout values
- [ ] **Ownership**: an agent can only finish a job it claimed itself
- [ ] **Expiry**: with the agent stopped, a queued job expires instead of running late when the
  agent returns; a probe answering minutes after the question is worse than no answer
- [ ] **Round trip on a real agent**: create → claim → execute → report → read result, and the
  UI states which agent produced the output
- [ ] **"Probe" on the IP detail page (identify)**:
  - only admins see the button; a read-only account calling `POST/GET /addresses/{id}/identify`
    gets 403
  - the target can only be that IP record's own address: the tools page's agent probe refuses
    `identify`; the agent itself refuses host names, multiple targets and networks
  - it runs on the scan agent assigned to the subnet; a subnet without one says so (not a blank
    failure)
  - one probe per IP at a time; every start is audited (action=identify)
  - the NSE script list is fixed inside the agent (read-only: banner / HTTP title / TLS
    certificate / SSH host key / SMB / RDP) and nothing the backend sends can change it; no
    industrial-protocol ports
  - run it once against a real PVE host: the type is hypervisor, 8006 is in the port list, and
    the names contain neither the certificate issuer nor wildcard names; an agent without nmap
    shows the "names only" notice
- [ ] **Probe by address (Probe in the anomaly lists, for an address IPAM has no record of)**
  (`e2e/anomaly-identify-cols-tabs.spec.ts`):
  - the address must be inside a managed subnet (the most specific one) and runs on that subnet's agent;
    an address outside every managed subnet gets `identify_not_managed`, a network/broadcast address
    `identify_bad_target`, and no job is created
  - overlapping subnets with the same CIDR handled by different agents → `identify_ambiguous`; never
    pick one and scan
  - a registered address goes to that record's probe page (same history); duplicate records must not be
    labelled "not in IPAM"
  - the task row carries the address, the completion notification links back to `/identify/ip/<address>`,
    the audit entry carries the subnet
- [ ] **Probe + Recog fingerprints** (`backend/tests/test_recog.py`, `e2e/ip-identify.spec.ts`,
  `e2e/recog-admin.spec.ts`):
  - import: every fingerprint must pass its own examples or it is dropped (about 5 in 3.2.0); only
    `xml/*.xml` is read from the zip, XXE is blocked, and a suspiciously small release (under 1,000
    fingerprints) never replaces the installed one
  - summary: the OpenSSH comment gives the distribution, a device default certificate gives type / vendor /
    model, "assert nothing" entries are ignored, ports nmap already named are not listed twice, and a default
    certificate's name is not listed as a host name; without Recog the summary is exactly as before and the
    page says it is not installed
  - on real data: re-summarise existing prod probe results with and without Recog; no type may get worse
    (NAS, PVE, mail host, IPMI)
  - **Admin → Recog fingerprints** (like the OUI database page): release, fingerprint count, install and last
    check times, per-file fingerprint counts (filterable); "Check for updates now" is audited
    (target=recog_db_update). Version info only lists the Recog release among the optional dependencies, with
    the name linking to this page; there is **no** update button on Version info
  - a failed update (GitHub unreachable) leaves the installed release alone and shows the error on the Recog page;
    the system diagnostics warn after three weeks without a successful check

## 7d2. Scan agent load: **whenever the agent's scan loop, its reports or the load evaluation change**

- [ ] **Liveness is never held up by heavy probes**: the agent reports each subnet as soon as its liveness pass
  is done; reverse DNS / NetBIOS / mDNS / OS fingerprinting run in the background and names do not wait for
  the OS fingerprint. On a real agent, `journalctl -u jt-ipam-scan-agent` shows each cycle's "probes=… alive=…"
  within seconds to tens of seconds, with `[heavy]` running separately
- [ ] Background results are **not evidence of being online** (`liveness=false`): they do not touch last-seen
  and never create IPs
- [ ] Cycle statistics land in `scan_agents.last_cycle` and `scan_agent_cycles` (kept 7 days); the Load column
  and panel on the Scan agents page show them
- [ ] Overload alerts: only after 3 consecutive cycles, sent once, plus once on recovery; the suggestions are
  actionable (which subnets to move, which subnet is unusually slow, which one is truncated)
- [ ] No automatic re-assignment: "Move to another agent" in the panel is an admin's click, with a reminder that
  the agent must be on the same network segment
- [ ] **Subnets over 4,096 addresses are scanned in rotating chunks**: assign a /19; each cycle covers the next
  chunk and wraps around (it used to scan only the first chunk forever); a full pass slower than the online
  threshold counts as overload with a suggestion to split the subnet (`tests/test_agent_scan_split.py`)

## 7e. Audit chain anchoring: **whenever audit writes, anchoring or the sync schedule change**

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

## 7f. Zabbix integration: **whenever the Zabbix sync or coverage gap changes**

- [ ] **Three URL forms**: `https://host`, `https://host/zabbix` and a full `api_jsonrpc.php` all connect
- [ ] **Both auth modes**: API token and username/password each tested; the read response carries no secret
- [ ] **Stamps existing addresses only**: a host in Zabbix that IPAM does not know must not create an IP
- [ ] **Scope**: with `scope_subnet_ids` set, the same IP in an overlapping range is not stamped onto
  another tenant's address; queries use `limit(1)` (`scalar_one_or_none` aborts the whole round)
- [ ] **Hostname convergence**: two Zabbix hosts pointing at one IP must not overwrite each other every
  round (the change log must not fill up)
- [ ] **Coverage gap**: asking with a subnet scope answers only for those subnets; an empty scope
  returns empty rather than falling back to global

## 7g. Evidence contract: **whenever a source is added or changed**

What this section guards: **a new source must answer whether its evidence expires**.
The cost of not having that gate has already been paid: ARP was treated as timestamped
evidence, and a machine powered off for weeks showed 52 days of green.

- [ ] **Registered**: the new source declares its tier and `aging` in `services/evidence.py`;
  `pytest tests/test_evidence_contract.py` is green (the guard rejects unregistered sources)
- [ ] **Tier is right**: passively learned mappings (ARP/FDB/DNS/DHCP/virtualisation config)
  are `learned` with `aging=False`; only active probes and third-party monitoring may age
- [ ] **No string matching for source semantics**: no `"scanner" in status` style checks
  remain; ask `evidence.is_aging()`, so a new source cannot fall into the loosest branch
- [ ] **Liveness settings**: the options and defaults are derived from the contract;
  a non-expiring source is **not** selected by default
- [ ] **Per-vendor evidence is labelled honestly**: a firewall's ARP table, VPN sessions and
  DHCP leases land in `ip_addresses.arp_seen` as `arp:<vendor>` / `vpn:<vendor>` /
  `lease:<vendor>`, **never** in `last_seen_scanner`. A site with no scan agent must never
  show "online (scanner)". `pytest tests/test_liveness_sources.py` green
- [ ] **Upgrade keeps the previous verdict**: firewall ARP was counted before the split
  (it was written as scanner evidence), so `arp:<vendor>` stays trusted by default;
  otherwise a firewall-only site goes entirely offline on upgrade. Leases do **not**:
  a lease can outlive the machine by days
- [ ] **Static ARP entries are skipped**: a permanent/static entry never ages out, so
  stamping it would mean "this host is alive forever"
- [ ] **Ghost-IP and ARP-only detection follow**: an address only a firewall can see is
  neither reported as a ghost nor as "ARP only"
- [ ] **Availability bar**: days backed only by ARP are grey, not green; carrying a state
  forward requires the source that state claims to still exist
- [ ] **Precedence**: all five attributes (hostname/MAC/OS/device name/model) take effect
  immediately after a change and disabled sources really are excluded;
  `pytest -k "precedence or hostname or arp"` green
- [ ] ⚠️ **Cache**: precedence uses a module-level 60s cache cleared between tests by
  `conftest`'s `bust_all()`. If the cache moves, **verify that fixture still clears it**;
  it once failed silently and tests leaked settings into each other

## 7h. IP lifecycle and cooldown: **whenever release, allocation or the cooldown setting change**

- [ ] **Release starts a cooldown**: after deleting an address it appears under
  `/addresses/cooldowns/{subnet_id}` with the previous hostname and MAC
- [ ] **The record survives deletion** (releasing an address in practice means deleting it)
- [ ] **Allocation skips it**: neither the free-address list nor automatic allocation offers it
- [ ] **Manual creation is refused**: recreating the address returns 409 with a readable
  message including the end date and previous hostname, **not** `[object Object]`
- [ ] **Early clear**: after clearing, the address can be used again, but the record remains
  with who cleared it, when and why (recorded, not erased)
- [ ] **Disabled**: setting 0 days restores the previous behaviour and writes no record
- [ ] **Purge**: the sync round removes long-expired records but **keeps recently expired ones**
  (the days right after expiry are exactly when someone asks who had the address)

## 7i. Event rules: **whenever rules, conditions or event dispatch change**

- [ ] **Conditions are not expressions**: confirm nothing is evaluated; regular expressions
  are **unsupported** (ReDoS)
- [ ] **An unknown operator never passes** (passing is the dangerous default)
- [ ] **Field paths walk data only**: `data.x.y` must not reach attributes
- [ ] **AND semantics**: every condition must hold; no conditions means the event name decides
- [ ] **A broken rule does not stop the others**: a malformed rule is flagged and skipped while
  the remaining rules and the normal webhook dispatch continue (**never silently inert**)
- [ ] **Dry run has no side effects**: it reports what would match without sending anything
- [ ] **The webhook action uses the same path**: signing and the SSRF guard cannot be bypassed

## 7j. Topology access layer (FDB): **whenever FDB inference or the topology map changes**

> FDB says "this MAC appeared on this switch port". Turning that into lines has two classic traps,
> and both of them draw a map that is confidently wrong rather than visibly empty.

- [ ] Access edges appear for hosts on ports carrying a single MAC, labelled with the port name.
- [ ] A port carrying more MACs than the threshold (an uplink/trunk) produces **no** access edges:
  the hosts beyond it are not drawn as plugged into that port.
- [ ] A port with several known hosts draws **dashed** edges (behind this port), not solid ones.
  Clicking such an edge shows "Directly attached: No" and the MAC count on the port.
- [ ] Two switches are joined only when each sees the other and the MAC sets behind the two ports
  are disjoint. In an A-B-C chain, **A-C must not appear**.
- [ ] A MAC that maps to more than one device (overlapping subnets) produces no edge at all.
- [ ] **Device-to-subnet links from ARP**: a switch or router whose LibreNMS ARP table holds addresses of a
  subnet is linked to that (most specific) subnet with ARP as evidence, and the subnet filter keeps it. These
  links never appeared from v0.4.29 until 2026-09-30 because `arp_entries.device_id` (a LibreNMS device) was
  compared with jt-ipam device ids; it must go through `LibreNMSDevice.jt_ipam_device_id`
  (`tests/test_topology_arp.py`)
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
  endpoint must either record an audit entry or be added to `EXEMPT` with a stated reason,
  never silenced just to make the test pass.
- [ ] **Rack diagram embedding**: after enabling it and generating a token in system settings,
  turn on one rack's toggle, copy the URL and open it in a **logged-out** browser; the image
  must render. A wrong or empty token returns 401; a rack that is not shared and one that does
  not exist return an **identical** 404; regenerating the token invalidates old URLs at once.
- [ ] **Basis of each link**: clicking any link shows its "basis" (recorded by a person /
  reported by monitoring / learned passively / guessed from the name). With "recorded only"
  on, just the recorded links remain; IP-to-device links survive in the subnet view, while
  the access-layer view may empty entirely, which is correct, and means nothing was recorded.
## 7k. Logic between related fields: **whenever a field that determines another changes**

> The rule: **if it can be derived from a relation we already hold, do not ask again**.
> The only case worth blocking is "both were given and they contradict each other", because
> then one of them is wrong and picking for the user would be a guess. A customer hit this
> once: selecting a rack still demanded a location, while the rack dropdown already reads
> "location / rack".

- [ ] **Device rack → location**: saving with only a rack chosen works, and the stored location
  is the rack's. Choosing both inconsistently is blocked with an explanation. A rack with no
  location of its own neither blocks nor invents one. **Test both entry points** (device list
  and the edit dialog on the device page): when the same logic exists twice, usually only one
  copy gets fixed.
- [ ] **Subnet → section**: adding a subnet from within a section carries the section over.
- [ ] **IP → subnet**: adding an address from a subnet page carries the subnet over and it
  cannot be switched to a different one.
- [ ] **VM → cluster / physical host**: a VM's node comes from the virtualisation platform,
  not from a human picking one.
- [ ] **Rack U position → rack height**: position plus size must fit the rack, and half-U
  devices (left/right) must not overlap on the same U.
- [ ] **Scan settings → scan agent**: enabling scanning without naming an agent is blocked;
  that is genuinely missing information, not something derivable.
- [ ] **Certificate agent → certificate scope**: an agent can only fetch certificates in scope.
- [ ] Before adding any "if A is set then B is required" rule, ask: **can B be looked up from
  A?** If it can, derive it; block only when it cannot.
- [ ] **The dependency list matches what is declared**: `pytest tests/test_dependency_page.py`
  is green. Adding any third-party package means updating the version page's list as well as
  `pyproject.toml` / `package.json`; that page is what an upgrade or audit checks against, and
  a missing entry raises no error, it just quietly is not there.

### 7.x Consoles and file transfer (WebSocket): walk this whole class by hand

This group comes from field reports across v0.5.222-229. What they share is that **the symptom
is always the same ("connection lost") while the cause is different every time**, so testing the
happy path of "an upload succeeded" is not enough.

- [ ] **Drag a folder in** (a folder alone, and a folder mixed with files): **the whole folder and
  its nested contents** must arrive with the same structure, files dropped alongside must still
  upload, and the connection must **not** drop. Do NOT decide "is this a file?" by `size > 0`: macOS reports a folder as
  **256 bytes**, and that check is what corrupted the stream.
- [ ] **Drag several files at once**: every one must arrive intact; compare **byte count and md5**
  for each. Only one arriving, or one arriving at **0 bytes**, means the upload loop was
  interrupted partway.
- [ ] **Send a command mid-upload**: if the client declares a size and then sends the next command
  before finishing, the server must **end that upload, still execute the command, and keep the
  session usable**. The command must not be swallowed.
- [ ] **Declare a size and send nothing**: after a timeout the server must report an error and
  clean up the partial file; it must **never wait indefinitely**. A coroutine parked there holds
  both the WebSocket and the SSH connection, and the user sees "the whole page is unresponsive".
- [ ] **The session survives a failed upload**: one failure must not force a reconnect.
- [ ] **Reconnecting right after a failure works**: the ticket request must not time out. A long
  delay means the previous session is still stuck in the event loop.
- [ ] **An idle console must not be cut off**: open SSH / SFTP / RDP / VNC / noVNC and **leave it
  untouched for three minutes**, then use it again. A console without a heartbeat gets dropped by
  an intervening reverse proxy after **60 seconds without traffic** (a common default), and the
  user just sees an inexplicable "connection lost".
  BMC currently has **no** heartbeat (pure relay; injected data would corrupt SOL), a known gap.
- [ ] **Send a file large enough to take a while** through the console (say 50 MB, or 5 MB over a
  slow link): it must complete. **Do not test this on the LAN only**: uvicorn drops a connection
  when no pong arrives within 20s, and the pong queues behind the upload data, so only a genuinely
  slow uplink reproduces it (fixed in v0.5.231). Browser network throttling **will not** show it:
  it does not put the pong behind the upload.
- [ ] **Guard tests green**: `pytest tests/test_sftp_upload_stall.py tests/test_ws_wait_timeouts.py`.
  They hold the line on three things that break uploads outright: a receive loop with no timeout,
  `receive_bytes()` raising KeyError on a text frame, and forgetting to send `put_ready` after open.
- [ ] Whenever the upload block changes, **actually transfer a file**. The 0.5.225 rewrite dropped
  the `put_ready` line entirely, uploads never started, and the symptom was **identical** to the
  bug being fixed, easy to read as "still broken" rather than "newly broken". Neither type
  checking nor unit tests can see this.

## 8. Recent feature spot-checks

- [ ] **OCS card shows OCS's own hardware** (device detail): manufacturer / model / serial come from OCS,
  not from device fields another source filled (a Windows device created by LibreNMS once showed
  "windows / Intel x64"); motherboard and BIOS rows; a factory placeholder system serial ("0123456789")
  is replaced by the motherboard serial and marked "(motherboard)"; main components list CPU (cores /
  threads), memory (total + modules), physical disks (no zram / loop), GPUs (lspci and driver entries
  merged). Before the first sync after upgrading, the card says the details come with the next sync
  (`e2e/ocs-device-card.spec.ts`, `tests/test_ocs_hardware.py`)
- [ ] **IPs without an agent can be filtered by status** (Wazuh and OCS pages): the status column is the
  same dot as the IP list and the filter uses the same rule; the options only list statuses present;
  exporting the list fills subnet / section / unit / status (these were blank before)
  (`e2e/missing-agent-scope-filter.spec.ts`)
- [ ] **Ports / cabling: the MAC column shows the vendor** under the MAC, like the IP list
  (`e2e/device-ports-mac-vendor.spec.ts`)

- [ ] **Notification matrix** (Admin → 通知發送設定): toggle events × (in-app / email); save persists; events fire
  per matrix (IP request, cert expiring/deployed/drift, anomaly).
- [ ] **Cert distribution `files` profile**: writes cert files only, no reload/restart.
- [ ] **Anomaly page**: tabs, per-table column picker, `ip_address_id` hidden by default (MAC drift: see the next list).
- [ ] **Anomaly status lights** (`tests/test_anomaly_liveness.py`): every tab with IPs has a Status column (same light as
  the IP list, per-source times on hover, sortable, exported as text); in MAC drifts each IP of a row gets its own
  light. The status is computed when the page opens: bring an IP online after the run and re-enter, the light must
  change. Unauthorized IPs (no IPAM record) are judged from ARP observations and list the MACs ARP saw (vendor,
  locally administered/random, who saw it) and the last-seen time.
- [ ] **Anomaly detection keeps the last result**: run once → go elsewhere and come back, the result is shown without
  running again, "Last run" shows when it ran, scheduled runs are marked. On the Unauthorized IPs tab click
  Identify then Back → the same tab with the result still there (`?tab=` in the URL).
- [ ] **Unauthorized IPs on a large site** (`tests/test_anomaly_scope.py::test_large_arp_tables_are_not_sampled`): with more
  than 2,000 ARP addresses, unregistered ones beyond the first 2,000 are still found; over 1,000 results the tab shows
  "Showing the N most recently seen of M" and the list is ordered by last seen.
- [ ] **Online/offline source in the IP change history** (#49, `tests/test_liveness_flip_source.py`): on a site without
  LibreNMS, an IP going offline is recorded with source `system`, one coming back online with the source that saw it
  (`scanner`, `opnsense`, ...); the source filter lists every integration.
- [ ] **MCP client-config generator** (LLM/AI): button outputs Claude Desktop / opencode / mcpo / generic snippets.
- [ ] **LLM provider = OpenAI-compatible** (Admin → LLM/AI): switching to it shows the data-egress warning
  and the API-key field; the model dropdown repopulates from `/v1/models` (empty dropdown = the wrong path
  is being called); a base URL already ending in `/v1` is not doubled; chat and semantic search both work.
  Switching back to Ollama restores the `/api/tags` list. `select value from system_settings where key='llm'`
  must show **no plaintext key**, only `api_key_enc`; the settings page never returns the key itself.
- [ ] **Embedding dimension** (Admin → LLM/AI): the **Check dimension** button reports the model's actual
  dimension against the column size. After changing the embedding model, a reindex must report
  `failed: 0`, and if it reports `0 indexed` the failure count and reason must be visible, never a bare
  zero. A candidate model must also produce **different vectors for different Traditional Chinese
  descriptions** (English-only models collapse them and look fine while ranking at random).
- [ ] **Add address in a subnet**: the create form has a required IP field (issue #14).
- [ ] **Attach IPs by NIC MAC** (Admin → 系統設定): off by default on an existing install; **Preview**
  reports a count plus per-reason skips and changes nothing; enabling it attaches on the next sync round
  and writes one IP-change-log row per address with the match reason. Clear a device link by hand, then
  confirm the next round does **not** restore it (the rule that keeps the job from fighting the operator).

### Recent (v0.6.45–v0.6.55 and not yet released)

- [ ] **MAC drift = a port change on the same switch** (2026-09-30; `tests/test_mac_drift.py`,
  `e2e/mac-drift.spec.ts`): a MAC seen on two switches is the normal path, not a move; only a new port on the
  **same** switch within 24 h (previous port within 7 days) counts. The table shows switch, from port, to port and
  when; only physical device moves are anomalies (and notify). VM migrations (a known VM NIC or a Proxmox
  address), randomised MACs and moves between shared ports go into the collapsed **Reference** block with their
  category and never notify. Anomaly subnet scope and per-IP ignore ("mac_drifts") apply. On prod data, compare
  the counts before and after one LibreNMS FDB discovery (FDB timestamps refresh every 6 hours)
- [ ] **AI interpretation model** (Admin → LLM/AI → AI interpretation; `tests/test_ai_interpret_model.py`,
  `e2e/llm-interpret-model.spec.ts`): empty = the chat model and its context length (an upgraded site behaves
  exactly as before). Set a different model and run all three: AI triage of an unauthorised IP, "Ask AI to read
  this" in an IP investigation (streamed), and the AI reading of a firewall rule change; the LLM server's log shows
  the chosen model, and each result names it. The review (audit) model is a separate setting and stays unchanged;
  embedding models are disabled in the picker
- [ ] **IP conflicts without LibreNMS** (#41; `tests/test_anomaly_ip_conflicts.py`, `tests/test_ip_conflict_evidence.py`,
  `e2e/anomaly-ip-conflict.spec.ts`): with no LibreNMS configured, scan agents and firewall ARP tables (dynamic
  entries only) still produce conflicts, tied to their subnet so overlapping networks never conflict with each
  other; a MAC flipping between two addresses 3+ times in 24 h is flagged; the AI tool says "cannot be determined"
  when there is no evidence
- [ ] **Anomaly filter** (`e2e/anomaly-filter.spec.ts`): one keyword (IP / hostname / MAC / details) filters every
  category and the tab counts read "matching / total"
- [ ] **Firewall rule rot** (`tests/test_fw_rule_rot.py`): OPNsense Anti-Lockout rules, port forwards to an alias
  and a WAN rule allowing only ICMP are **not** reported; any → any means every protocol and every port; the
  table has a Firewall column and the kind in words
- [ ] **PFX export password** (`e2e/cert-pfx-export.spec.ts`, `tests/test_certificates_api.py`): choosing PFX asks
  for a password twice (empty allowed, with a warning); the export is a POST with the password in the body; check
  the nginx access log and the browser history: **no password in any URL**; a GET carrying a password is refused;
  the file opens with that password on Windows
- [ ] **Address ranges (pools) inside a subnet** (#40; `tests/test_ip_ranges.py`, `e2e/subnet-ranges.spec.ts`):
  non-CIDR start–end ranges inside the subnet, no overlaps; size / used / next free (clicking it creates that IP);
  DHCP-pool ranges count as DHCP ranges everywhere (usage, "in a DHCP range", AI tools); audited; carried by
  system transfer. **Detected DHCP ranges appear automatically** marked "Auto" with their source, follow upstream
  (replaced, removed with the integration), never touch manual ranges, are skipped when the subnet is ambiguous or
  the range would overlap, cannot be edited by hand, and are not counted twice (`tests/test_ip_ranges_auto_dhcp.py`)
- [ ] **Rack kinds and drawing** (`tests/test_rack_more_kinds.py`, `e2e/rack-more-kinds.spec.ts`,
  `e2e/rack-side-channels.spec.ts`, `e2e/rack-room-align.spec.ts`): slotted angle steel shelving (presets, finish),
  IKEA KALLAX (square cells, frame thicker than dividers), LackRack (8U per table, 50 mm legs outlined); shelves use
  one scale for width and height; cable space on both sides from the outer width (465.1 mm hole spacing), top panel
  and base with thickness; a room row has one toolbar (front/rear, size slider, export) in both separate and merged
  layouts. **Compare screen, SVG / draw.io export and the embed image**: three implementations.
  At desktop width there must be **no** horizontal scrollbar under any rack (check with macOS set to always show
  scrollbars; KALLAX had one because its width was measured rounded)
- [ ] **IP detail page**: firewall rules, aliases and NAT rows click through to that vendor's page with only that
  entry shown (a banner offers "show all" and explains a missing entry); MikroTik address lists appear under
  "member of aliases" and `list:<name>` rules trace back to the IP (`tests/test_fw_lookup_aliases.py`,
  `e2e/ip-firewall-aliases.spec.ts`); the
  relation chart runs physical on the left, logical on the right, like the device page
- [ ] **OCS** (`tests/test_ocs_integration.py`, `tests/test_ocs_agent_tabs.py`, `e2e/ocs-agent-tabs.spec.ts`,
  `e2e/missing-agent-scope-filter.spec.ts`): the page has the Wazuh tabs (one agent row per computer); a subnet
  scope limits MAC matching (empty = global) and "IPs without an agent" lists only the union of the enabled
  integrations' scopes; the list filters by section / subnet / unit and export follows the filter; a container
  whose old agent (2.4.2 or earlier) marks every NIC virtual still matches its IP
- [ ] **Consoles**: when the remote host ends an RDP / VNC session the console says so, and an RDP session that
  ends before any screen lists the server-side causes (`tests/test_rdp_remote_ended.py`); the FreeRDP engine
  leaves no `xfreerdp` / `Xvfb` behind after 20 connect / disconnect cycles (`ps` before and after); where aardwolf
  cannot be installed the installer, the RDP/VNC error and system settings name the Python version and point to
  another engine; after "remember" on noVNC / BMC the saved-credential list shows the name, not a UUID
  (`e2e/novnc-saved-cred.spec.ts`); a PVE console login failure says why: wrong realm lists the realms, a
  rejected login names host and account, unreachable gives the cause (`tests/test_pve_login_errors.py`)
- [ ] **Reasoning models on OpenAI-compatible servers** (#36; `tests/test_llm_reasoning_control.py`): against
  llama.cpp with a thinking model (see the llama.cpp test target), AI audit / triage get an answer instead of
  spending the whole output limit thinking; a reply cut off while thinking says so instead of "(empty response)"
- [ ] **MCP over HTTP** (`tests/test_mcp_url_and_audit.py`): `POST /api/mcp` and `POST /api/mcp/` both reach MCP
  (the bare URL, the one the manual and the client-config generator give, used to return 405); a mutating
  tool called through `tools/call` writes an audit entry `mcp_tool_exec` (tool, summary, channel, source IP);
  configure a real MCP client (mcp-remote) from Admin → LLM/AI and run one read and one write
- [ ] **Audit entries name the actor** (`tests/test_audit_actor_recorded.py`): create a user, change a group's
  members and edit an OPNsense / Wazuh integration; each audit row has the acting admin (20 sites used to
  record nobody; `request.state.user_id` is now set by `get_current_user`)
- [ ] **AI tools are never looser than the REST data they read** (`tests/test_mcp_tools_match_rest_permissions.py`):
  as a wildcard-read non-admin, ask the AI chat for Wazuh agents, OCS computers, scan agents and certificates; each is
  refused, like the REST pages
- [ ] **Rack embed from another site**: put `<img src="https://<host>/api/v1/racks/<id>/embed.svg?token=…">` on a page
  served from a different origin; the image renders (the response carries `Cross-Origin-Resource-Policy:
  cross-origin` and no 30-day `Expires`); change the rack and reload, and the image changes
- [ ] **Behind nginx** (fresh install and upgraded site): `curl -k https://<host>/readyz` returns the backend's JSON
  (it used to be the SPA's index.html with 200) and turns 503 when PostgreSQL is stopped; repeated failed
  phpIPAM logins `POST /api/phpipam/<app_id>/user/` get 429 after the burst, with `Retry-After: 60` and a JSON
  body; after an upgrade the site has `location = /readyz`, the regex phpIPAM location and `location @rate_limited`
  (`patch_nginx_readyz_phpipam`, idempotent; check it in a real nginx container with mawk, as Debian has)
- [ ] **Thinking controls through an LLM gateway** (LiteLLM etc.; `tests/test_llm_reasoning_control.py`): when the
  server rejects one of `reasoning_effort` / `chat_template_kwargs` / `thinking_budget_tokens`, only the one the
  error names is dropped (LiteLLM rejects `thinking_budget_tokens` but turns `reasoning_effort: "none"` into
  Ollama's `think:false`), and the rejection is remembered per server and model so later requests do not fail
  first. With a real LiteLLM in front of a thinking model, an AI triage reply comes back in seconds, not minutes
- [ ] **Wazuh / OCS pages load fast on a large site** (`e2e/agent-tabs-lazy.spec.ts`): the "IPs without an
  agent" list (and Wazuh's full agent list) is fetched only when its tab is opened; the tab still shows the agent
  count on page load
- [ ] **"IPs without an agent" is paged on the server** (`tests/test_missing_agents_paged.py`,
  `src/composables/__tests__/useRemoteMissing.test.ts`, `e2e/missing-agent-scope-filter.spec.ts`): the tab fetches one
  page (not tens of MB); section / subnet / unit / status filters, the text filter and every column sort go to the
  backend and cover all gaps, not just the page on screen; picking a section narrows the subnet menu and clears a subnet
  from another section; the status light and the status filter agree with the IP list (same rule, compared in a test);
  export downloads everything that matches the filters; after an agent is installed the IP drops out on the next
  refresh. On the scale dataset (53k gaps) the first open takes about a second or two, paging well under one
- [ ] **AI chat can turn thinking off** (`tests/test_chat_thinking_setting.py`): Admin → LLM / AI has a "Let the model
  think before answering in AI chat" switch (on by default = old behaviour). Off: Ollama gets `think:false`, OpenAI-compatible
  servers (LiteLLM, vLLM, llama.cpp) get the thinking-off fields, official OpenAI gets none; a server that rejects one
  field still answers (only that field is dropped, and remembered). With a thinking model, turning it off makes the
  "thinking" stage disappear and replies come back noticeably sooner
- [ ] **Error responses keep their headers** (`tests/test_http_error_headers.py`): a `401` carries
  `WWW-Authenticate: Bearer`; a backend `429` carries `Retry-After` (60 for the rate limiter, 900 for the login lockout)
- [ ] **Device type from the periodic OS probe** (`tests/test_device_identity.py`, `tests/test_anomaly_identity_changes.py`,
  `e2e/recog-device-identity.spec.ts`): an agent 1.14.0 periodic OS result fills the IP's OS, device type and model
  (judged like the IP probe, Recog included); an old agent's one-line OS still works; a camera that turns into a
  Windows host shows under Anomaly → "Device type or OS changed" with old → new (unknown → known and A→B→A are not
  listed; Ignore this IP works); the IP list's Device type column (column picker) and the IP details show it; a
  topology device of unknown type takes its primary IP's kind. Run the agent's nmap path for real once (nmap in a
  container against a container target); unit tests mock nmap
- [ ] **An overruled fingerprint does not lend its type or vendor** (`tests/test_recog.py`, `tests/test_device_identity.py`):
  when Recog names a different OS than the nmap fingerprint with high confidence (e.g. fingerprint says HP storage, Recog
  says Linux), the device type follows the OS (server) and the vendor is not HP; VMs and containers matched by a
  virtualization integration never take type or vendor from the fingerprint (no role-specific service → server /
  windows), and the IP "Probe" page summary agrees with the periodic probe; an already misjudged "Storage · HP" becomes
  "Server" on the next probe without keeping HP as model (`test_ip_identify.py`). On production check a PVE LXC
- [ ] **MikroTik lease hostnames** use their own source, not "manual" (`tests/test_hostname_reports.py`): a typed hostname
  is not overridden, and the lease hostname disappears when the lease does

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
- [ ] **Scan-agent OS detection** (agent ≥ 1.7.0): appliances/BMCs are no longer mis-guessed; Debian
  appliance (SSH banner) → `Debian`, Windows via SMB/Service-Info → `Windows`, device-model-only guesses
  (NAS / OpenWrt / router) are dropped to unknown rather than shown.
- [ ] **Notification i18n**: switch UI language (繁中 ⇄ English) → the bell **and** the Notifications page
  render in the current language (IP-request, anomaly, cert, stale-IP); old notifications fall back to stored text.
- [ ] **Notification channels** (Admin → 通知發送設定): Telegram / Slack / Teams / Nextcloud Talk / Zulip each
  save (encrypted token/webhook; a saved value shows as set and is kept when the field is left blank), the per-channel **Test** button delivers, and an
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
