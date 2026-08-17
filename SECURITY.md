# Security Policy

If you believe you've found a security issue in FERAL, please report it
privately to the maintainers before disclosing publicly. FERAL is a
single-trusted-operator, local-first AI agent — the threat model is
small and explicit, and the bar for what counts as a vulnerability is
shaped by that model.

## What FERAL is

FERAL is a personal-assistant brain that runs on the operator's
machine. There is exactly **one** trusted operator per running brain.
The operator owns the host, owns the credential vault, owns every
paired device, and owns every tool the brain may invoke. FERAL's
security boundaries protect this operator from external prompt
injection, from adversarial channel ingress, from sandbox escape by
tool-genesis-generated code, and from supervisor or twin policy
bypass — they are **not** designed to keep the operator from acting
against their own machine.

## In Scope

The following are vulnerabilities in the brain itself and are in scope:

- **Remote code execution (RCE)** in the brain process from any
 externally reachable surface (HTTP API, `/v1/node` WebSocket, a
 channel adapter ingress, the GenUI iframe, an MCP request).
- **Credential theft / vault exfiltration** — anything that lets an
 external party read `~/.feral/credentials.enc`, the vault master key,
  or paired-device tokens. The vault is encrypted at rest with
 ChaCha20-Poly1305 and pairing tokens are Argon2id-hashed.
- **Sandbox escape** — code running inside the sandbox image
 (`Dockerfile.sandbox` or `Dockerfile.sandbox-browser`) reaching the
 host filesystem, host network namespace, host PID namespace, or
 another container.
- **Supervisor bypass** — any path that lets an action reach an
 orchestrator entry point (`handle_command`, `handle_command_stream`,
 `handle_ui_event`, `handle_daemon_result`) without the audited
 Supervisor wrapper, or that lets a paused brain still execute.
- **Vault tampering** — undetected modification of the encrypted vault
 blob. The AEAD tag must catch this; `VaultTamperedError` must be
 raised.
- **Channel adapter abuse against the operator** — a malicious payload
 on Slack / iMessage / Telegram / email that crosses an auth,
 allowlist, approval, or sandbox boundary and acts on the operator's
 behalf.
- **Twin / executor approval bypass** — calling a registered
 `TwinExecutor` or a high-risk MCP tool without the per-domain or
 per-tool consent record (`agents.twin_policy.TwinPolicyEngine`,
 `security.exec_approvals.ApprovalManager`).
- **Subagent allowlist bypass** — invoking
 `agents.subagent_spawner.spawn_subsession(parent_id, child_kind, …)`
 with a `child_kind` that is not in `agents.subagent_policy`'s default
 allowlist (or operator-supplied override) and reaching the runner.

## Out of Scope

The following are **not** vulnerabilities and reports about them will
typically be closed as `invalid` / `no-action`:

- Issues the operator can introduce by acting on their own machine —
 e.g. compromised host OS, compromised browser profile, malware the
 operator installed under the same OS user as the brain.
- A FERAL skill, channel adapter, or tool-genesis-installed package
 that the operator authored or installed themselves performing
 privileged actions. Skills are part of the operator's trusted
 computing base; see "Plugin trust boundary" below.
- Credentials stored *outside* the FERAL vault by the operator
 (`OPENAI_API_KEY` exported in the operator's shell, secrets in
 `~/.config/something-else.json`, plaintext keys in `.env` files
 the operator chose to keep around).
- "The operator can shut down the brain" / "the operator can revoke a
 paired device" / "the operator can edit `MEMORY.md`" — these are
 intentional operator-control surfaces, not bugs.
- Prompt-injection-only chains where no auth, policy, approval, or
 sandbox boundary is crossed. The model is **not** a trusted
 principal; defenses come from boundaries, not from the model.
- Reports against the demo / scenarios fixtures, dev tooling under
 `dev/`, or test harnesses under `feral-core/tests/`.
- Reports requiring write access to `~/.feral/`. Anyone who can write
 there is already a trusted operator.
- Reports that depend on a multi-tenant deployment where mutually
 untrusted users share one brain instance. FERAL is not designed for
 that and does not try to provide per-user isolation.
- Heuristic / parity drift between exec surfaces (e.g. a deny rule
 applied to one surface but not another) that does **not** demonstrate
 a concrete bypass of an in-scope boundary.
- Reports that depend on the operator setting `FERAL_AUTONOMY_MODE=loose`
 or a similar break-glass flag. Those are explicit operator-selected
 trade-offs.

## Pairing access posture (Mode A / B / C)

FERAL distinguishes three reachability stances; the operator picks one
in `/setup` or in Settings → Access. Each has a different network
attack surface.

**Mode A — Local (LAN, "same WiFi").** Brain binds `0.0.0.0`; pair
URL is `http://<lan-ip>:<port>/pair?t=<token>`. Anyone on the same
LAN can reach the brain socket. Defenses:

- The pair URL embeds a single Argon2id-hashed pairing token (24h
 sliding TTL). Without the token, even an on-LAN attacker cannot
 open `/v1/node`.
- HTTP routes outside the open allowlist (`/health`, `/api/devices/pair/{url,qr,complete,announce,status,code/claim}`,
 `/install-phone-bridge.sh`, `/pair`, `/v2/pair`, hashed `/assets/*`)
 return 401 to non-loopback clients without a Bearer.
- The browser-served `/pair?t=…` page is intentionally open so a
 freshly scanned phone can land on it without a pre-existing token;
 it MUST then be redeemed via the WebSocket handshake which calls
 `verify_device(token)` against the Argon2id verifier.
- Mode A is appropriate for trusted LANs (home, single-tenant office).
 It is **not** appropriate on coffee-shop / hotel / conference WiFi;
 the pair modal surfaces the AP-isolation caveat verbatim.

**Mode B — Localhost.** Brain binds `127.0.0.1`. The dashboard, the
desktop wrapper, and the CLI talk to it; **no pair URL is emitted**.
The "Pair Device" button is disabled with a tooltip. This is the
default for fresh installs.

**Mode C — Remote (Tailscale Funnel).** Brain binds `0.0.0.0`;
public URL is the Tailscale Funnel URL (`https://<machine>.<tailnet>.ts.net`).
Tailscale handles transport encryption (WireGuard) and tailnet ACLs;
the brain still requires a valid pair token at the application layer.
Notable properties:

- No port-forwarding, no domain registration, no certificate the
 operator manages.
- Tailscale's relay network proxies through CGNAT.
- Operator authenticates to Tailscale once via OAuth (Google / GitHub
 / Apple / email) — FERAL never sees the credential.
- Remote-mode pair URLs that resolve to a loopback address (operator
 misconfiguration) are **rejected** at emit time with a 409 telling
 the user to run `feral access remote-up` or set
 `FERAL_PUBLIC_BASE_URL` correctly.

### Pair-code flow brute-force resistance

The `POST /api/devices/pair/code/claim` endpoint accepts an 8-character
base32 code (~38 bits of entropy). With:

- 600-second TTL on each pending code,
- 5 wrong attempts per source IP per 15 minutes (sliding window;
 see `feral-core/api/middleware/rate_limit.py`),
- 10 wrong attempts per code → server-side invalidation (anti-correlation),

the expected cost to brute-force a single live code is on the order
of decades of sustained traffic, well beyond any single-trusted-operator
deployment's threat profile. The limiter is process-local in memory;
brain restart resets counters.

### `?api_key=` query authentication is deprecated

The brain still accepts `/v1/node?api_key=<token>` for the deprecation
window (sunset `2026.7.0`); each accept logs a structured
`feral.security.deprecated_query_auth` warning. Web-side history caches
that include the URL leak the token; that is the deprecation rationale.
All in-tree clients (web, extension, phone-bridge, both SDKs, both
mobile apps of record) are migrated to `Authorization: Bearer`.

## Threat model — single-trusted-operator boundary

FERAL runs on **one** machine for **one** operator. That operator's OS
account is FERAL's trust boundary; everything inside it is trusted,
everything outside it is untrusted (and crosses one of the documented
ingress paths: `/v1/node`, HTTP API, WebSocket, channel adapter, MCP).
The primary defenses against the in-scope risks are:

1. **Sandboxing** — tool-genesis code, GenUI app code, and
   subagent worker code execute inside `Dockerfile.sandbox` /
 `Dockerfile.sandbox-browser` containers with `--cap-drop=ALL
 --network=none --read-only` plus a watchdog. Dropped capabilities,
 no network, no host filesystem mount, non-root `feral` user.
2. **Vault encryption + token hashing** — credentials live in
 `~/.feral/credentials.enc` (ChaCha20-Poly1305, master key in the
 OS keychain, recovery code shown once at first boot). Pairing
 tokens are Argon2id-hashed; legacy plaintext rows are migrated to
 `needs_rotation_log` on first boot.
3. **Supervisor audit + kill switch** — every orchestrator entry point
 is wrapped (`agents.supervisor.Supervisor`); every call is recorded
 to `supervisor.db` with `decision=allowed/denied/queued/error`; a
 single `set_paused(True)` blocks every dispatch.
4. **Per-tool / per-domain approvals** —
 `security.exec_approvals.ApprovalManager` for high-risk tools and
 `agents.twin_policy.TwinPolicyEngine` for twin domains. Both are
 default-deny without an explicit consent record; per-session grants
 never promote across sessions.
5. **Subagent allowlist** — `agents.subagent_policy.is_allowed`
 is default-deny; the orchestrator can spawn only the small set of
 child kinds in `_DEFAULT_ALLOWLIST`. Denials are audited via
 `supervisor.record(kind="subagent_spawn", decision="denied")`.

The approval-bypass test family
(`feral-core/tests/security/test_*_approval_bypass.py`) demonstrates
that each of these boundaries holds against an attempted bypass — not
just that the API returns 403, but that the bypassed call never reaches
the underlying side effect AND the supervisor sees a denial event.

## Reporting

Email **security@feral.sh** with reproduction steps, affected
component(s), and a clear impact statement. A PGP key fingerprint will
be published in this section by the maintainer; until then, reports
that need to ship sensitive payloads should request the public key in
a first contact email.

Required in reports:

1. Title and severity assessment.
2. Affected component (file path + commit SHA you tested against).
3. Reproduction steps that work against the current `main`.
4. Demonstrated impact tied to one of the in-scope categories above.
5. Suggested remediation, if any.

Reports without reproduction steps or demonstrated impact will be
deprioritized.

### Fast-path triage gate

Reports that demonstrate any of the following are triaged at **HIGH**
within one business day:

- [ ] Credential exfiltration — vault, OS-keychain master key, pairing
 tokens, channel adapter API keys, or any vault-backed secret.
- [ ] Sandbox escape — code in `Dockerfile.sandbox` or
 `Dockerfile.sandbox-browser` reaching the host filesystem,
 host network namespace, or host PID namespace.
- [ ] Supervisor bypass — an action reaching an orchestrator entry
 point without audit, or executing while the supervisor is
 `paused=True`.

Everything else is triaged at **NORMAL** (target: one week to first
substantive response).

## Common false-positive patterns

The following report shapes are commonly filed but are **not**
vulnerabilities under FERAL's trust model:

- "The operator can shutdown the brain by killing the process." —
 intentional, the operator is trusted.
- "I configured `FERAL_AUTONOMY_MODE=loose` and the brain executed a
 dangerous tool without prompting." — operator-selected break-glass.
- "The brain ran a skill the operator installed and that skill made
 HTTP requests / wrote to the filesystem / read the vault." — skills
 are trusted plugins.
- "An LLM with prompt injection produced text that references the
 operator's email address." — the LLM is not a trusted principal;
 context visibility is not, by itself, an authorization boundary.
- "I sent a malicious string in a channel and the model `replied` to
 it." — out of scope unless the reply crosses a tool, approval, or
 sandbox boundary.
- "I can `docker exec` into the brain container as root." — that
 requires root on the host, which already collapses the operator
 boundary.
- "I supplied a custom regex in `~/.feral/config.yaml` that
 catastrophically backtracks." — operator-supplied configuration;
 hardening at best, not a security boundary bypass.
- "The HTTP API accepts requests from `127.0.0.1` without an auth
 header." — local loopback is the trusted-operator surface; bind to
 loopback only and rely on OS user isolation.

## Plugin trust boundary

Skills, channel adapters, and tool-genesis-installed packages are part
of the operator's trusted computing base. Once installed, they run
in-process with the brain and have the brain's OS privileges. Reports
that show a malicious operator-installed plugin doing privileged things
are out of scope. Reports that show an *unauthenticated* path that lets
a remote party install or invoke a plugin **are** in scope.

Within that boundary, one asymmetry is enforced rather than assumed: a
skill manifest may **escalate** its own safety verdict but may not
**de-escalate** it unless the manifest ships in this repository.

- `safety_tier: confirm`, `safety_tier: deny` and
 `requires_user_approval: true` are honoured from any manifest. The
 worst outcome is an extra prompt.
- `safety_tier: safe` and `read_only_hint: true` are honoured only from
 a manifest under `feral-core/skills/manifests/`, or one generated at
 runtime from a currently paired device by
 `feral-core/hardware/capability_skill.py`. From anything installed at
 runtime under `~/.feral/skills/` they are ignored with a log line, and
 the tool resolves through the danger map and the substring heuristic
 exactly as an unannotated third-party skill would.

Enforced in `feral-core/security/safety_resolver.py`
(`_manifest_may_de_escalate`), which covers `resolve_policy` (the
approval gate on every surface), `is_read_only` (the strict-autonomy
skip in `agents/tool_runner.py`) and `is_read_only(strict=True)` (plan
mode). Before this, an installed skill declaring itself `safe` executed
with no confirmation on every surface. The same trust boundary already
governed the declared `result_budget` in `skills/result_budget.py`.

A third-party manifest that declares nothing is unchanged: it falls to
the substring heuristic, which can still return `auto` for a name
containing `search`, `get`, `list`, `read`, `status` or `current`. That
heuristic is a legacy fallback, not a boundary; do not rely on it.

### Getting into that boundary: install verification and consent

Because an installed plugin runs with the brain's privileges, the
decision that matters is the install itself. Every install path that a
non-CLI user can reach is verified and consented to:

| Path | Verification |
|---|---|
| `feral install <id>` | `cli/install.py`: SHA-256 of the downloaded tarball must match the registry record, and the publisher's detached Ed25519 signature over that digest must verify. A mismatch exits non-zero before anything is written. |
| `POST /api/marketplace/preview` then `POST /api/marketplace/install` | The same `cli/install.py` `_verify` and `_safe_extract`, called from `skills/marketplace.py`. Not a second implementation. |
| `POST /api/apps/preview` then `POST /api/apps/install` | `agents/app_registry.py` `stage_registry_bundle` + `install_staged_registry` for a registry bundle, `inspect_app` + `install_app` for a directory or git checkout (GenUI apps, separate bundle format). Both halves run the same gate, `AppRegistry._verify_source`. |
| `MarketplaceClient.install(source_url=...)` | **None.** Developer local-iteration path, logs an `UNVERIFIED INSTALL` warning, and is refused by the HTTP route (403). Reachable only from a Python caller. No CLI command reaches it either: `feral install` and `feral marketplace install` both take a published registry id and run the verifier. |

The marketplace install is two steps, and the token that joins them is
what makes the consent binding:

1. `POST /api/marketplace/preview {kind, id}` fetches the registry
   record, downloads the bundle, verifies digest + signature, unpacks it
   into a staging directory and reads the manifest **out of the verified
   archive**. The `manifest` field of the registry's JSON response is
   not covered by the signature, so it is metadata, not evidence. The
   response carries the permission list, the signature status and a
   single-use `install_token`. Nothing under `~/.feral` is touched.
2. `POST /api/marketplace/install {kind, id, install_token}` spends the
   token and installs the staged tree. Without a token the request is
   refused with 403.

The token is an HMAC-SHA256 over `{kind, id, sha256, permissions,
verified, nonce, iat, exp}` keyed by a random secret generated per
process and never persisted. It cannot be replayed for a different
package (the id and the bundle digest are inside the MAC), cannot be
replayed twice (the nonce is dropped when spent), expires after five
minutes, and does not survive a brain restart. Because the install
consumes the *staged* bundle rather than re-downloading, the registry
cannot serve different bytes between the preview and the install.

A bundle that does not verify never produces a token, so the permission
list is only ever rendered for bytes whose publisher is known. Package
validation (`SkillValidator`) also runs on the staging copy, before the
tree is copied into `~/.feral/skills/`.

Permissions themselves are a closed vocabulary
(`models/skill_manifest.py::SkillPermission`). A manifest naming a
permission outside it fails to load, so a publisher cannot describe a
capability in words the consent dialog has no sentence for.

### Installing an app installs code, so it asks the same way

A GenUI app is a bundle of SDUI surfaces rendered inside a sandboxed
iframe, so its own risk is data reach rather than in-process code, and
`genui/permissions_policy.py` governs that: `permissions.network` becomes
the iframe's CSP `connect-src`, and a wildcard grant needs a signed
manifest, a publisher justification and `user_high_trust=true`.

That is not the whole install. An `AppManifest` may declare
`skill_dependencies`, and a **skill runs Python in the brain process** at
load time (`skills/registry.py` `_try_load_dynamic_impl` calls
`spec.loader.exec_module`). Until v2026.8.16 `POST /api/apps/install`
required no token and resolved those dependencies by calling
`MarketplaceClient.install(skill_id, "latest", None)`, the unverified
developer path. Installing an app was therefore an unattended install of
code-executing packages with no signature check and nothing on screen.

`POST /api/apps/preview` now performs the whole install except the
writing:

1. The bundle is fetched and verified. A registry bundle goes through
   `stage_registry_bundle` (SHA-256 + detached Ed25519 over the tarball);
   a directory or git checkout goes through `inspect_app`, which runs
   `AppRegistry._verify_source` — the same method `install_app` runs, so
   a preview that says "this will install" and an install that then
   refuses cannot drift apart. The verified tree is *kept*, and the
   install writes it rather than fetching again.
2. The app's own reach is rendered as consent copy by
   `agents/app_registry.py::describe_app_permissions`.
3. Every declared skill dependency is resolved into one of three buckets:
   already installed, will be installed (verified through
   `MarketplaceClient.preview_from_registry`, so its permission list
   comes out of a bundle whose publisher signature checked out), or
   cannot be installed.
4. A single-use `install_token` is minted with
   `skills/marketplace.py::sign_consent_token` — the same HMAC over the
   same per-process secret the marketplace uses. The app token carries
   the *skill* tokens of its dependencies, so the outer MAC covers them
   and a dependency cannot be swapped after the list was read.

`POST /api/apps/install` spends the token. Without one it returns 403
`preview_required`. The token cannot be replayed, cannot be redirected to
a different source, and expires after five minutes. Dependencies are
installed with `MarketplaceClient.install_from_registry`; the unverified
`install` is unreachable from any route, and
`tests/test_app_install_consent.py` AST-scans `api/routes/` to keep it
that way.

**A dependency that cannot be verified does not refuse the install.** It
is disclosed with the brain's own reason, the actions in the manifest
that will stop working (`skill_dependency_impact` reads the surfaces'
`skill_call` action targets), and a remediation that has been checked to
be accurate — no command is offered where no command would help, which
is why a signature failure prints none: every install path runs the same
verifier. The user then chooses. The app installs without that skill,
`/api/apps` recomputes `missing_skill_dependencies` on every listing, and
the Apps page keeps showing the shortfall until the skill is installed.
A dependency that *did* verify at preview time and then failed to install
is a different case: that is a broken invariant rather than a choice, and
the app is rolled back.

## Sandbox policy enforcement points

`~/.feral/policies/default.yaml` (`SandboxPolicy`) is read at these
points. Anything not listed here is not enforced anywhere:

| Policy section | Enforced by |
|---|---|
| `hardware.sensors` | `POST /api/hardware/execute` |
| `hardware.actuators` | `POST /api/hardware/execute` |
| `hardware.cameras` | `POST /api/hardware/execute` (capture capabilities) |
| `network.allowed_domains` / `blocked_domains` | the generic HTTP skill runner, `feral-core/skills/executor.py` |
| `mcp.*` | `MCPClientManager.connect_server` and `connect_all` |
| `filesystem.*` | the computer-use file tools and `security/exec_mode.py` |
| `execution.allow_shell_commands`, `daemon.shell`, `daemon.applescript` | `validate_shell_command` / `validate_applescript` |

Two properties of that table are deliberate:

- **An empty allowlist denies.** `hardware.sensors.allowed`,
 `hardware.actuators.allowed`, `hardware.cameras.allowed` and
 `mcp.allowed_servers` used to read an empty or absent value as "allow
 everything", while `network.allowed_domains` in the same class read it
 as "allow nothing". A policy document does not have to be complete, so
 an operator who wrote one section had the rest wide open. All four now
 deny. `mcp.allowed_servers` takes `["*"]` (the shipped default) to
 mean "any server", because server names are operator-chosen and the
 shipped policy cannot enumerate them.
- **The network allowlist binds third-party skills, not first-party
 ones.** The shipped manifests name fixed URLs that are reviewed source
 in this repo and legitimately reach dozens of vendors; holding them to
 an operator's six-entry allowlist would break every integration and
 the available fix would be to write `*`. `network.blocked_domains`
 binds everyone, first-party included.

`hardware.movement.max_speed_pct`, `permissions.max_tier`,
`permissions.require_confirmation_above` and `skills.allow_generation` /
`require_approval` / `blocked_skill_ids` currently have **no** reader.
They are configuration that does nothing; do not rely on them.

## Shell execution modes

A shell request resolves to exactly one of three modes, decided by
`feral-core/security/exec_mode.resolve_execution_mode` from the command,
the resolved working directory, the operator's autonomy mode, and the
grant state in `~/.feral/workspace_grants.json`:

- **`docker`**: mandatory for anything that runs generated code, namely
 `code_interpreter`, `workspace_scripts`, tool-genesis output, and any
 endpoint declaring `requires_sandbox: true`. A workspace grant does
 **not** substitute. If Docker is unavailable the request is refused and
 names `docker` as the mode it needed.
- **`host_workspace`**: the operator's own developer shell
 (`coding_tools__bash`), run on the host with its cwd inside a folder
 the operator granted. Under `FERAL_AUTONOMY=strict` the cwd must be
 covered by an explicit grant; under `hybrid` / `loose` a path the
 filesystem policy already declares readable also qualifies. Path
 arguments named on the command line are checked against the same
 `SandboxPolicy` the file tools use, so a path denied to
 `coding_tools__read_file` is not reachable via `cat`.
- **`refused`**: everything else, including any cwd outside every
 grant, any `blocked_paths` hit, and `execution.allow_shell_commands=false`.

Executing on the host inside an explicitly granted folder is **in the
threat model as designed**, not a bypass: per "What FERAL is" above, the
boundaries protect the single trusted operator from prompt injection and
untrusted skill code, not from their own machine. A report showing the
brain running a shell *outside* every grant, or running generated code on
the host in any mode, **is** in scope.

`daemon://local/shell` remains separately gated by
`SandboxPolicy.validate_shell_command` (argv[0] allowlist plus
metacharacter reject), and `daemon://local/applescript` by
`SandboxPolicy.validate_applescript`, which also validates the `-e`
payloads of any `osascript` invocation reaching the shell allowlist.

## Sandbox image hygiene

The two shipped sandbox images (built from `Dockerfile.sandbox` and
`Dockerfile.sandbox-browser`, both `FROM` `Dockerfile.sandbox-common`)
are versioned via
`feral-core/security/sandbox_image.SANDBOX_IMAGE_VERSION`, which
embeds a sha256 of the three Dockerfile contents. Any change to those
files changes the image tag — the launcher in
`feral-core/security/docker_sandbox.py` will only run the image whose
tag matches the brain's pinned `SANDBOX_IMAGE_VERSION`, so a partial
upgrade can never produce a brain talking to a stale sandbox recipe.

References:

- Internal comparative architecture analysis — sandboxing + security
 audit notes live in the `docs/` private analysis tree.
- mission statement: single-trusted-operator threat
 model (this document).
