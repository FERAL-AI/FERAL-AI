# Local iMessage, browser and glasses task execution

Research date: October 2, 2026. This is one feature inside FERAL. No feature code,
account connection, message sending or payment was performed for this report.
Recommendations below are engineering decisions; third-party capabilities are
documentation claims until the named integration tests pass.

**Verdict: reuse FERAL's running agent, browser tools, memory and authorization.
Add an iMessage transport and a narrowly scoped payment adapter.** This does not
require a replacement brain or a new application. The purchase helper currently
stops at a price preview, so actual checkout requires new execution and outcome
handling. See [release readiness](RELEASE_READINESS.md) and the
[iOS handoff](IOS_AGENT_HANDOFF.md) for the common prerequisites.

**1. iMessage: prefer a native `imsg` adapter on supported Macs; support existing
BlueBubbles installations as an optional transport. Keep SIP enabled.**

`imsg` offers a smaller local dependency: a Swift CLI with structured watch/send
and stdio RPC. Its normal workflows read `chat.db` and use Messages automation.
It requires macOS 14+, so the feature must be version-gated rather than silently
raising FERAL's macOS 13 deployment target. BlueBubbles supplies REST/webhooks and
handles message database decoding, but adds a separate Electron server. A custom
`chat.db` plus AppleScript bridge would make us maintain decoding, database/WAL
watching and OS changes ourselves. AirMessage is another Mac relay, but provides
less reason to choose its client/server stack for this local agent adapter.
[imsg overview](https://imsg.sh/),
[imsg installation and supported systems](https://imsg.sh/install.html),
[BlueBubbles implementation](https://github.com/BlueBubblesApp/bluebubbles-server/blob/master/packages/server/package.json),
[BlueBubbles database/automation design](https://docs.bluebubbles.app/server),
[BlueBubbles API](https://docs.bluebubbles.app/server/developer-guides/rest-api-and-webhooks),
[AirMessage source](https://github.com/airmessage/airmessage-server).

- **Confirmed here:** FERAL has `desktop.messages.send` in
  `feral-core/skills/desktop_control/facade.py`, using Messages.app AppleScript.
  There is no iMessage receive adapter among the channel implementations. Existing
  channels already normalize messages and enforce inbound sender allowlists.
- **Docs/source only:** normal `imsg` read/watch/send needs Full Disk Access and
  sending needs Automation permission; advanced typing/read-receipt generation
  uses an injected helper. BlueBubbles private features also require disabling
  SIP. No bridge installation, live permission check or send/receive was tested.
  [imsg permissions](https://imsg.sh/permissions.html),
  [imsg private-feature boundary](https://imsg.sh/advanced-imcore.html),
  [BlueBubbles private API](https://docs.bluebubbles.app/private-api/installation).
- **Blockers:** define the agent's address. A bridge speaks as the Mac's signed-in
  Messages identity; it does not create a separate FERAL contact automatically.
  The preferred person-like thread needs a dedicated agent identity on a Mac
  user/session, or an explicitly limited self-chat mode. Allow only enrolled
  owner addresses and one selected thread; ignore agent echoes, group chatter,
  reactions and historical backfill as new commands. Persist source GUIDs and
  cursors. Recover with reconciliation rather than resending uncertain messages.
  Mac sleep/logout disconnects local availability. Do not promise typing/seen
  states that the chosen basic transport cannot actually observe or produce.

**2. Stripe Link: use each user's own Link wallet through the local CLI first;
keep FERAL's exact-action approval in addition to Link authorization.**

The documented CLI connects a consumer account through a verification URL and
phrase. Create a request with seller, items and total; request approval; poll;
retrieve a one-time card or supported checkout token; pay; reconcile the result.
Full card output must bypass model transcripts, logs and screenshots containing
payment fields. The CLI provides private-file output and isolated auth paths.
[Link CLI](https://github.com/stripe/link-cli).

The payment-only CLI path does **not** require a Stripe business account; payment
users must currently be US or Canadian consumers. Financial insights is a
different capability with additional authorization/account requirements.
[Stripe availability and authentication](https://docs.stripe.com/agentic-commerce/agents/link-agent-wallet).
The hosted/branded OAuth flow **does** list a Stripe account and client application,
and issues a confidential client secret. Do not embed that secret in an open-source
desktop binary. Confirm a supported branded public-client/device flow with Stripe
before promising native branded sign-in.
[Stripe client registration](https://docs.stripe.com/agentic-commerce/agents/link-agent-wallet/oauth).

- **Confirmed here:** `security/commerce.py` has spend screening and an audit
  trail; `web_actions.make_purchase` returns `purchased:false` and explicitly has
  no checkout. No Link adapter or real payment has been verified.
- **Docs only:** independent local installations can authenticate their own Link
  wallets; eligibility and payment-method checks still apply. Link offers test
  credentials without charging the underlying method. Its payment guide documents
  intent idempotency and payment/step-up states.
  [Stripe payment flow](https://docs.stripe.com/agentic-commerce/agents/link-agent-wallet/use-link-wallet-pay-online).
- **Blockers:** upstream sources disagree on the approval window: repository
  README says 30 minutes, payment guide says 10. Use returned expiry/status, not a
  hardcoded duration; verify the pinned CLI/API in sandbox. Wallet approval is
  separate from merchant order confirmation. Never repeat checkout after a lost
  response without checking payment and order status. Price/address/seller drift
  invalidates FERAL approval. Handle decline, verification, 3DS, cancellation and
  refunds explicitly. A one-time card does not guarantee every merchant accepts
  it. Keep optional financial-data scopes outside payment onboarding.

**3. Browser: keep FERAL's existing CDP/Playwright executor. Evaluate upstream
Browser Use as an optional planner, not a second unrestricted agent.**

Playwright supplies dependable low-level actions, waiting and browser contexts;
it does not independently decide how to complete arbitrary tasks. Browser Use
supplies a model-driven browser agent and can run locally, but must still execute
through FERAL's policy and task ownership. Benchmark it against the existing
orchestrator before adding another dependency/control loop.
[Playwright action checks](https://playwright.dev/python/docs/actionability),
[Browser Use local library](https://github.com/browser-use/browser-use).

- **Confirmed here:** `skills/impl/browser_use.py` is **FERAL's own** controller,
  not the upstream Browser Use package. It already connects Playwright to Chrome
  over CDP and can launch an isolated `FERAL_HOME/chrome-profile`.
  `web_actions.py` already calls it. Browser domain-memory/error fixtures were
  exercised in the 89-test check below; no live merchant or Google account was used.
- **Docs only:** CDP attachment is Chromium-only and lower fidelity than a native
  Playwright connection. Stored authenticated state is sensitive.
  [Playwright connection limits](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp),
  [Playwright authentication](https://playwright.dev/python/docs/auth).
- **Blockers:** isolate tabs and profiles by task/account; do not seize the user's
  everyday browser or expose its debugging port remotely. Page content is
  untrusted instruction input. Login/2FA/CAPTCHA and merchant changes require
  clear human handoff. Use existing scoped Google OAuth integrations for supported
  APIs; use an explicitly connected browser account for web-only tasks. Do not
  infer access to every Google service from one granted scope. Redact traces and
  authenticate secret-field filling outside the model-facing tool surface.

**4. Glasses: use the existing Theora phone relay and HUP; correlate a new capture
with the spoken request, then continue the same task through iMessage.**

The intended route is camera/microphone glasses → Theora iPhone → authenticated
FERAL node connection → existing vision/voice pipeline → owned task → iMessage
status and review. The health-temple glasses and Theora Eye camera glasses are
distinct devices; health sensing does not establish camera/mic availability.

- **Confirmed by source inspection:** Theora's `FeralGlassesRelay.swift` already
  invokes `TheoraEyeManager.captureForAssistant`, receives a JPEG notification
  and sends a vision frame. `AmbientTranscriptSync.swift` already has a durable
  transcript outbox and accepted/duplicate ACK handling. FERAL's
  `api/server.py`, `perception/glasses_buffer.py` and `context_attach.py` ingest
  and attach image context. Focused software fixtures passed; no glasses or
  physical iPhone test was performed.
- **Important gaps found:** the phone capture notification is not bound to the
  originating capture ID; the local camera-handler guard does not reject an
  empty grant list (outer-path authority still needs testing; this is not a
  demonstrated end-to-end authorization bypass); the vision attach path requests the freshest frame across
  **any** device with `device_id=None`. These are unacceptable assumptions for
  buying the particular item you are looking at. Fix exact capture/node/owner/turn
  correlation and granted-capability handling before enabling checkout.
- **Source locations:** `<theora-ios-checkout>/ios/Theora/Feral/FeralGlassesRelay.swift`
  lines 442 and 545–576; `feral-core/perception/context_attach.py` line 362.
  These were read directly during this investigation, not inferred from a
  physical-device test.
- **Proposed:** persist owner, task, conversation, source node, capture ID and
  frame hash/time; attach only the matching recent image. If unclear, ask which
  product/variant. Transport ACK, image capture, user approval, payment and
  merchant receipt remain different states. The ambient outbox is reusable
  design evidence, not an existing durable purchase queue.

**5. Architecture: adapters around the existing brain, with one durable task
record shared across app, glasses and messaging.**

| Existing area | Reuse | New work |
| --- | --- | --- |
| `channels/base.py`, channel manager | normalized messages, sender gate | iMessage adapter, cursors, GUID deduplication, truthful delivery events |
| `agents/orchestrator.py`, `tool_runner.py`, `security/exec_approvals.py` | tool routing and session approvals | persistent task lifecycle, exact reviewed terms and channel-safe approval correlation |
| `skills/impl/browser_use.py`, `web_actions.py` | browser actions and previews | verified checkout/reservation executors and merchant outcome reconciliation |
| `security/commerce.py`, vault, OAuth manager | caps, audit, credential boundaries | Link subprocess adapter, per-owner auth, credential-only execution lane |
| memory store | context and factual history | bounded task summaries/receipts with provenance; exclude payment secrets |
| HUP models/server, glasses buffer, Theora relay | authenticated device transport, camera/audio | capture/turn correlation and selected-device grants |
| native app and headless contracts | existing control and review surfaces | same pending task/inbox/API receipts everywhere, no UI-only authority |

**Confirmed here:** these modules exist and were inspected. This table is a
proposed extension map, not a claim that the new endpoints or durable commerce
task lifecycle are implemented. Open-source users retain explicit, inspectable,
revocable autonomy choices; a channel must not silently expand their permissions.

**Test record.** The harmless local command below passed **89 tests** in 3.29s
with disposable storage; it covers existing vision ingestion/context and browser
domain-memory behavior, not iMessage, Link, hardware or a real checkout. An initial
run without an explicit disposable home hit four database-open failures in the
node fixtures; the isolated rerun passed. Existing warnings include deprecated
FastAPI/Starlette APIs and a restored test environment-variable leak.

```sh
cd feral-core
FERAL_HOME=/private/tmp/feral-instinct-research-fixtures-20261002 \
  ../.venv/bin/python -m pytest tests/test_vision_context_attach.py \
  tests/test_vision_entry_points.py tests/test_browser_domain_memory.py \
  -q --no-cov -p no:randomly
```

**Proposed build sequence, after approval of this feature report:**

1. Agree task/owner/event/approval contracts with desktop and iOS. Complete the
   persisted execution/unknown-outcome states and exact vision correlation first.
2. Add version-gated `imsg` and optional BlueBubbles adapters. Test a disposable
   agent account: receive, reply, attachments, duplicate events, unauthorized
   senders, restart, sleep/reconnect and uncertain sends. Do not read all personal
   message history into memory by default.
3. Complete one real browser workflow through FERAL policy. Test login handoff,
   navigation, cancellations, parallel tab ownership and exact merchant outcomes.
4. Add Link using pinned CLI schemas and per-owner auth. Exercise test-mode
   approvals, expiry, denial, changed totals, step-ups and uncertain checkout.
   Ensure card fields never reach prompts, normal logs, traces or memory.
5. Connect physical Theora Eye voice/camera to the same task. Test wrong/stale
   images, concurrent captures, empty grants, locked phone and network loss.
6. Run the genuine end-to-end workflow and publish evidence, support boundaries
   and troubleshooting. A real-money purchase requires the user's exact approval;
   this research report authorizes none.

**60-second cafe acceptance idea:** with accounts and the Mac already connected,
look at a cafe's packaged coffee and say “get me that.” FERAL shows the captured
product in the iMessage thread, finds that cafe's actual online store, and asks
you to confirm variant, pickup location and final total. Approve the Link request;
FERAL checks out and sends the merchant's order number and pickup instructions.
Show the same task in the Mac app. Run this first against merchant/Link test mode;
only label a later real purchase completed when its merchant receipt is verified.
Sixty seconds is a presentation target, not a promised checkout latency. This
acceptance journey checks the real product, rather than substituting a demo for
its release gates.

This report completes the requested research-only stage. Implementation and live
account/payment tests wait for the user's go.
