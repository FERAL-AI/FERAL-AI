# Mac, Home and messaging integration repair

October 9, 2026. This extends the existing FERAL runtime. Source checks,
packaged checks and physical-device outcomes remain separate.

## Reproduced causes and changes

Home remained implemented, but commit `6fbe8b62c` redirected `/` to Chat and
removed Home from the pinned dock. Root and `/ambient` now lead to `/home`;
Home is pinned again and `/chat` remains available. The generated WebUI served
by the backend is rebuilt from those sources.

The native host selected a random port unless FERAL_PORT was present. That
ignored saved network.port and could invalidate a phone's paired endpoint on
restart. The host now resolves FERAL_PORT, FERAL_BRAIN_PORT, saved network.port,
then 9090, in that order. Invalid configuration refuses startup instead of
silently changing the endpoint. Settings reads are bounded to 1 MiB. Listener
access policy and instance-authenticated process ownership remain unchanged.
This is a confirmed launch defect, not proof of the cause of a particular phone
failure. Saved LAN access still requires an explicit setting and restart.

Native setup's October 5 change requested live model discovery while retaining
cached provenance text. Passive suggestions now request live=false with
recommended/chat filters and explicitly describe cached or bundled results.
An explicit probe remains distinct from displaying suggestions. Backend
runtime_supported/setup_selectable flags are not yet fully consumed by every
client; full provider selection parity is not established.

The interrupted iMessage adapter is completed as a **disabled admission
foundation**, not a usable transport. A future connector must implement the
documented FERAL companion envelope and provide a real receipt store/transport.
The boundary validates HMAC, timestamp, account, stable event identity, sender
and thread allowlists; it rejects unsigned events, groups, malformed direction markers, outgoing echoes, invalid/oversized text and attachment-only tasks.
SQLite claims persist before entering the existing channel handler. Duplicate
or uncertain work is not replayed. Changed terms refuse, storage is bounded,
and bounded cooperative stop retains unsettled handler/database operations with unknown-outcome diagnostics. A successful send attempt
is only reply_unverified, never delivered or seen. Receipt records contain
digests/state rather than message text. Neither imsg nor BlueBubbles wire
compatibility, receiving, attachments, actual sending or read receipts is
accepted by these tests. Startup reports the transport unavailable.

## Executed evidence

| Check | Result | Limit |
|---|---|---|
| Home, route and navigation Vitest, seven suites | 81 passed | App route table and page fixtures |
| WebUI production build and core asset synchronization | Passed | No inference |
| Source backend viewed through Computer Use in Chrome | Root setup completed; `/home` rendered with Home pinned, 43 skills, one session and live status | Disposable profile, no real provider or action |
| Native runtime-port selector | 176 assertions passed | Environment/settings fixtures and actual disposable regular/symlink/FIFO reads |
| Native onboarding setup | 77 assertions passed | Controlled request/provenance fixtures |
| Native production typecheck and linked runtime-port checks | Passed: 28 model groups, 39 desktop, five error assertions | No physical phone or microphone |
| Actual BrainRuntime/launcher ownership fixture | 30 assertions passed, including identical saved endpoint across stop/relaunch without environment port override | Actual processes with a synthetic health-only backend, not the full installed GUI |
| Channel, phone authorization and existing-Chrome contracts, ten suites | 244 passed, one skipped, 119 warnings; 16.32 seconds | Inert boundary effects, isolated data |
| Playwright dock geometry | Not executed: required Chromium headless shell missing | Ten launch failures preceded test bodies; not product assertion failures |

The initial native setup command failed at the sandboxed compiler-plugin
boundary. Its unsandboxed focused runner passed 77 assertions, but its linked
compile was invalidated by concurrent review edits. Final frozen RuntimePort
linked checks and production typecheck passed on the integrated sources.
This failed attempt is retained separately. The first isolated backend could not bind
inside the sandbox. Reusing a null-keyring test profile then failed vault-key
recovery; a fresh disposable profile started successfully. These are retained
test-environment failures, not acceptance of a personal vault or installation.

Small private logs: feral-oct9-contract-tests-hardened.log,
feral-oct9-native-setup-unsandboxed.log, feral-oct9-web-build.log and
feral-oct9-source-webui-b.log. Generated applications, private profiles and
logs are excluded from source control.

## Remaining end-to-end gates

1. Build a new exact-source 9.47 candidate, preserving 9.46 as the sole rollback.
   Verify packaged source, dependencies, signature and startup/shutdown.
2. Launch the actual candidate with a disposable saved-port profile and no
   FERAL_PORT override; quit and relaunch, asserting the same endpoint.
3. Test an explicitly paired Theora phone against the reviewed LAN endpoint.
   A selected Mac-chat Chrome attachment is not automatically the phone's
   canonical session: establish an owner-controlled shared-task handoff without
   bypassing browser ownership or local-only connection routes.
4. Select and test an actual version-pinned iMessage connector, then wire receipt
   lifecycle, authenticated transport, reply outcomes and restart reconciliation.
   The existing channel handler currently scopes sessions by sender and omits
   account/thread/event identity; its empty-result fallback can say Done.
   Activation must repair that existing-executor handoff and truthful outcomes.
   Current source makes no external Messages operation.
5. Preserve the broader voice, providers, local provisioning, durable jobs,
   desktop ownership and distribution gates in RELEASE_READINESS. These narrow
   repairs do not establish full-product readiness.
