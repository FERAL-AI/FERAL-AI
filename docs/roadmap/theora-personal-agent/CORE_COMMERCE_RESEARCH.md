# Theora personal agent: core, commerce, network, memory, and desktop research

Read-only source investigation and primary-source research, 2026-09-30. No implementation changes, live provider calls, purchases, deployment, or messages. All architecture below is a proposal unless explicitly marked implemented. OpenAI Docs skill was used for current official OpenAI documentation. Source links describe documentation availability, not Theora production eligibility.

## Principal finding

FERAL provides much of the agent runtime, tools, permissions, memory, device protocol, and local execution foundation. It does not currently constitute a production consumer application or transaction engine. The first product milestone should be one person's reliable continuity across Theora iPhone and Mac/Linux, with explicit execution authority and health provenance. Buying things requires a separate deterministic transaction service, not another model prompt or generic browser approval.

## Implemented foundations and important gaps

### Desktop

- `ASOS/desktop/src-tauri/src/main.rs`: local Python brain process ownership, runtime resolution, diagnostics, bounded health probes, shutdown, tray, shortcuts, floating chat, autostart plugin.
- `ASOS/desktop/scripts/stage_bundle.sh`: relocatable pinned interpreter and brain payload, FTS5 verification, built web UI verification, dependency/import smoke, guards against builder-path leaks.
- `ASOS/desktop/src/main.js:140`: main user surface is an iframe of the local React dashboard. This is a native shell around a web UI, not a native consumer information architecture.
- `ASOS/.github/workflows/desktop.yml`: experimental, manual-only debug build matrix; explicitly not yet release assets.
- `ASOS/desktop/src-tauri/tauri.conf.json`: localhost-only CSP, signingIdentity null, empty updater public key. `Cargo.toml` does not include updater plugin initialization dependency. Config alone therefore does not provide a functioning secure updater.
- Initial native shortcut handling dispatched a DOM event on the outer window. A jsdom reproduction confirmed it never reached the iframe. The bounded fix now forwards an exact versioned message to the loopback brain frame; the client validates parent/origin and uses its normal voice-start path. Automated regressions pass; actual packaged hotkey/microphone behavior remains untested. See VERIFIED_DESKTOP.md.
- Remote brain selection is environment-based; previous URL/API-key form was removed because values were unused. A product-grade authenticated local/remote selector remains needed.

### Identity and memory

- Agent identity/personality from identity YAML and USER/SOUL/MEMORY Markdown: `feral-core/agents/identity_loader.py`.
- Cryptographic brain identity via Ed25519 key in vault and derived relay ID: `security/brain_identity.py`. This proves brain-key possession; it is not a consumer account or same-owner device model.
- Device credentials and peer-brain credentials have different powers: `security/device_pairing.py`, `security/peer_roster.py`.
- `memory/sync.py` implements mDNS/static peers, WebSocket exchanges, HLC/vector clocks, LWW/append/union rules, tombstones, chunking, retry/backoff, per-peer scopes.
- `security/sync_scopes.py`: `private` never replicates, enrollment grants no memory by default, grants authorize named scopes in both directions. Revoking sharing cannot recall existing remote copies.
- Sync allowlist (`memory/sync.py:1504`) includes notes, episodes, conversations, knowledge, wiki pages, execution logs, entities, relations. This is not full replication of identity files, credentials, settings, jobs, purchase ledger, approvals, and health histories.
- Current single-operator trust assumption is explicit in `ASOS/SECURITY.md`; hosting many customers requires actual account/tenant isolation beyond a shared brain instance.

### Remote access

- `services/relay_client.py`: outbound NAT tunnel; separate untrusted listener avoids publishing loopback authentication exemptions; observable reconnect state.
- `feral-relay/edge/feral_relay_edge/broker.py`: SNI-based routing and raw encrypted byte forwarding; edge does not terminate client TLS.
- Relay client docstring explicitly states it has never been run against a real edge and has local WebSocket test coverage. Treat as implementation with unproven production integration, not working global connectivity.

### Commerce

- `skills/impl/web_actions.py`: initial executable verification confirmed navigation, first-candidate price extraction and cap screening without cart/checkout/payment. A bounded correction now returns an explicit read-only product preview: `purchased:false`, `awaiting_confirmation:false`, `checkout_available:false`, and `price_verified:false`. Descriptions no longer promise cart totals or checkout.
- Initial generated purchase cards had unregistered Confirm/Cancel IDs. Dispatching both through the actual UI handler verified silent no-ops. Those purchase buttons have now been removed; no payment authorization is created. Generic booking-card behavior is outside this bounded correction.
- `security/commerce.py`: configured currency, transaction and rolling-24-hour caps, merchant allowlist; unknown price/currency and unset caps denied. SQLite `purchases.db` trail stores amount, merchant, tool, session/approval references, reason and outcome.
- Ledger has no checkout version/hash, order/payment references, idempotency key, reserved spending, fulfillment/refund state. `spent_today` counts only completed entries. Direct fault injection reproduced zero-on-read-failure and silent-write-failure defects; bounded fixes now refuse unavailable-ledger budget decisions and raise failed writes, with three new regressions. No completed record call in inspected runtime commerce code. It is a preliminary evaluation trail, not settlement/accounting.
- `security/exec_approvals.py`: tool/session/permanent grants and in-memory pending requests. Tool permission is not authorization for a particular immutable cart/amount/address.
- `security/trust_ledger.py`: purchases deliberately excluded from earned trust because money cannot be undone. Preserve that property; refunds are compensation, not guaranteed undo.
- Generic browser click/type/evaluate, shell, MCP and external-agent paths need action-level commerce enforcement. A cap inside `web_actions` alone cannot prove no other tool can submit a checkout. This is a review requirement, not a claimed exploit demonstrated here.

## Current external mechanisms: specification versus real access

### ACP and OpenAI

ACP is Apache 2.0 and describes programmatic commerce. Merchants retain control and can reject particular agents or transactions; implementing ACP does not automatically list products or grant access to all merchants. Discovery remains an explicit integration concern. [ACP site and FAQ](https://www.agenticcommerce.dev/), [ACP documentation](https://www.agenticcommerce.dev/docs).

Current official OpenAI docs describe product feeds and approved-partner onboarding. They are merchant-to-ChatGPT integration documentation, not an API promising that an independent Theora agent can use ChatGPT's entire merchant network. [Official OpenAI getting started](https://developers.openai.com/commerce/guides/get-started).

Proposal: build an ACP-compatible adapter behind a local transaction interface, pin schemas, and integrate only explicitly available merchants. Do not mistake existing FERAL/OpenCode ACP (Agent Client Protocol) coding-agent bridges for commerce ACP; same abbreviation, unrelated purpose.

### Stripe

Agentic Commerce Suite agent integration is private preview for physical goods in specified countries. It requires verified agent onboarding and a seller-initiated orchestrated commerce agreement before seller connection. Catalog `disable_checkout` distinguishes discovery-only items. Native payment collection uses mobile SDK; web uses Elements. Stripe also raises possible marketplace-facilitator tax duties. [Embedded integration](https://docs.stripe.com/agentic-commerce/for-agents.md?agent-checkout-mode=full).

Shared Payment Tokens bind a seller profile, currency, maximum amount and expiry. They can require further customer actions such as 3DS, can be revoked, and have non-linear state transitions. A token becoming active is not proof of completed fulfillment. [Agent SPT guide](https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens.md?agent-seller=agent).

Proposal: prefer an approved PSP-hosted payment collection/token route to exposing card data to the model. Provider access, merchant agreement, test/live eligibility and geography are external gates. Open source code can include adapters and simulators without distributing PSP secrets or falsely guaranteeing access.

### AP2

The fetched current AP2 specification is v0.2: Checkout Mandate and Payment Mandate, linked to a merchant-signed checkout hash; deterministic verification; non-agentic Trusted Surface; direct and autonomous constrained modes. Catalog/commerce API details and dispute retention mechanics are outside scope. Agent-to-agent mandate delegation is outside this current spec. This differs from the 2025 launch blog's Intent/Cart terminology, so pin versions instead of designing against a historical blog alone. [Current specification](https://ap2-protocol.org/ap2/specification/), [implementation considerations](https://ap2-protocol.org/ap2/implementation_considerations/).

Proposal: borrow the invariant of binding informed consent to the actual checkout, and implement AP2 only where counterparties support the pinned version. A signed mandate is neither a merchant catalog nor a payment processor, consumer account, or guarantee of acceptance.

### Visa and Mastercard

Visa Trusted Agent Protocol uses cryptographic agent identification/intent evidence for merchants; Visa Intelligent Commerce requires agent onboarding for its services. Public specification availability is distinct from approved credentials, issuer/card support and merchant acceptance. [Visa TAP](https://developer.visa.com/capabilities/trusted-agent-protocol), [Visa Intelligent Commerce](https://developer.visa.com/capabilities/visa-intelligent-commerce/overview).

Mastercard describes registered agents, network tokens, authenticated user intent and explicit consent in Agent Pay. This public page does not establish Theora's production enrollment or universal merchant availability. [Mastercard Agent Pay](https://www.mastercard.com/us/en/business/artificial-intelligence/mastercard-agent-pay.html).

Proposal: use PSP integration initially if it handles network provisioning; direct card-network integrations should follow a concrete business/technical onboarding arrangement.

### Apple Pay boundary

Apple's authorization controller gets payment selection and USER authorization, after which the framework creates a payment token. Apple Pay being supported by a PSP does not give a background agent authority to press through biometric/wallet approval. [Apple authorization guide](https://developer.apple.com/library/archive/ApplePay_Guide/Authorization.html).

Proposal: Theora can prepare a cart and open an approved native payment sheet; the user performs required wallet/issuer interactions. Screenless glasses must direct the user to a trusted phone screen when visual confirmation/authentication is required. Any future delegated purchasing uses separately established mandates and payment credentials, not inferred Apple Pay permission.

## Proposed deterministic purchase state machine

Use a dedicated PurchaseService called by all clients and tools. The model proposes intent, item discovery and cart construction; deterministic code owns authority and execution.

1. `DRAFT`: intent, constraints, user identity, source surface. No payment authority.
2. `QUOTING`: merchant adapter obtains a concrete checkout, version, line items/SKUs/variants/quantity, subtotal, tax, shipping, discounts, total/currency, fulfillment address/options, subscription recurrence, expiry, returns/cancellation terms.
3. `AWAITING_CONFIRMATION`: persist canonical immutable terms and hash, merchant verified identity, quote expiry and exact consent text. Bind request to owner, purchase ID and checkout version, not arbitrary UI action strings.
4. `AUTHORIZED`: authenticated trusted-surface approval or valid narrowly scoped delegated mandate. Require fresh consent after material terms changes. Atomically reserve budget, check caps INCLUDING outstanding reservations, and claim single execution lease.
5. `PAYMENT_ACTION_REQUIRED`: present official wallet/issuer flow. Model cannot provide authentication or rewrite approved terms. Expiry/revocation invalidates unused approval.
6. `SUBMITTING`: persist attempt ID and provider idempotency key BEFORE side effect. Reuse key for the same immutable operation; never create a fresh purchase because a client retries or reconnects.
7. `OUTCOME_UNKNOWN`: timeout/disconnect after submit. Keep budget reserved; communicate uncertainty. Reconcile by checkout/order/payment IDs, status API and signed verified webhook events. No blind retry with a new key.
8. `CONFIRMED`: verified merchant order acceptance AND appropriate payment state; attach receipt and durable external IDs. Model-generated narrative and a screenshot are not receipts. Track authorization/capture and fulfillment separately.
9. `DECLINED`, `EXPIRED`, `CANCELED_BEFORE_SUBMISSION`: terminal when externally supported evidence confirms no outstanding obligation; release reservation accordingly.
10. Post-order: `FULFILLING`, `SHIPPED`, `DELIVERED`; independent `CANCEL_REQUESTED`, `REFUND_REQUESTED`, `PARTIALLY_REFUNDED`, `REFUNDED`, `DISPUTED`. External confirmation determines refund outcome. Preserve original order and append compensating events.

Persistent records should include purchase/owner IDs, terms hash/version, consent/mandate hash and expiry, budget reservation, action lease, provider idempotency keys, request attempts, external references, event dedupe IDs, receipts, refund references, and explicit unknown status. Secrets/tokens stay in vault/PSP structures, never semantic memory. One owner-wide ledger is needed to prevent Mac and phone separately passing the same daily cap.

Financial ledger reads/writes and authorization persistence must fail closed for spending. Receipt/audit logging cannot be “best effort” once money may move. Exact-once distributed execution cannot be promised; achieve durable idempotency plus reconciliation and explicit unresolved outcomes.

## Proposed personal continuity and network architecture

Start with one authoritative personal brain, accessed by native iPhone and desktop clients, plus local capability executors. Preserve a self-hosted option; optionally offer an isolated hosted personal-brain deployment for iPhone-only users. Hosted availability must not silently upload local health/memory: present deployment/data-flow choices explicitly. Laptop asleep means local brain unavailable unless an explicit always-on/hosted option exists.

Define separate principal types: person/owner, brain, client device, hardware peripheral, delegated agent, external peer. Enrollment proves ownership and assigns capability grants. Lost-device revocation and recovery are product features, not just manual token rotation.

Same-owner private continuity needs a separate authorized encrypted replication mechanism or access to the same authoritative store. Do NOT make existing `private` scope grantable or mark all health data shareable to work around peer scopes. Shared knowledge across owners should remain explicit scopes/consent with provenance and no expectation of recalled copies. Learning routing/skill reliability does not automatically imply cross-user model training.

Record health observations separately from inferred personal facts: source sensor, device, measurement and arrival timestamps, units, signal quality, model/calibration version, confidence, validity interval and correction history. Use stale/missing/uncertain states. A derived or disputed fact must not become unconditional prompt identity. Current peer sync does not guarantee health-record replication.

Network release gate: LAN/cellular switching, captive portals, NAT, background suspension, sleep/wake, outages, relay authentication, TLS/certificate issuance-renewal, per-device revocation, key recovery, bounded retry/backpressure and observability. Test real deployed relay end-to-end, not solely local mocks.

## Proposed desktop product and distribution work

Retain Tauri as the baseline unless measured product requirements justify a rewrite. Bundle the consumer client directly rather than an iframe of an administrative dashboard; share React UI and versioned protocol while using native adapters for menus, notifications, clipboard/file dialogs, microphone, shortcuts and system settings. Native feel is an interaction/accessibility criterion, not a requirement that every widget be Swift/AppKit. Theora iOS stays native with protocol parity, rather than forcing all desktop pages onto phone.

Primary UI: conversations, memory you can inspect/correct, personal tasks, health insights with provenance, connected glasses/devices and pending actions. Advanced runtime/admin panels remain available progressively. Wire shortcut→actual voice state, not log-only notification.

Release plan: build per-supported architecture, relocatable brain and dependencies, no-checkout/no-Python clean install, stable per-user data directory, single-instance/port contention handling, crashes/relaunch and resource limits; sign/notarize Mac application and nested binaries; signed updater key management and verification; explicit Linux packaging support (start AppImage plus one supported Debian-family package, then broaden based on evidence). Verify X11/Wayland shortcuts/tray/microphone differences rather than promising parity.

Official Tauri supports platform installers, describes Mac Developer ID signing/notarization and Linux signing, and requires updater signatures. These are distinct trust chains; signing the update does not replace Mac notarization or package repository trust. [Distribution](https://v2.tauri.app/distribute/), [Mac signing](https://v2.tauri.app/distribute/sign/macos/), [Linux signing](https://v2.tauri.app/distribute/sign/linux/), [Updater](https://v2.tauri.app/plugin/updater/).

## Prioritized implementation proposal and evidence gates

**P0 — Before consumer promises or any live autonomous spend:** choose brain availability/ownership model; establish versioned client/session/approval/device contracts; real Theora sensor-to-brain provenance; persistent exact-action approvals; commerce coordinator with immutable terms/budget reservation/idempotency/reconciliation; route every spend-capable execution path through it; real authenticated relay trial; clean-install packaging and privacy/recovery design.

**P1 — Private alpha:** one polished desktop and existing Theora iOS journey, same conversation/tasks/memory across clients, lost-device revocation, offline drafts, honest pending/unknown indicators, native voice/notifications, observable performance. Commerce initially discovery and trusted checkout handoff; test-only merchant simulator exercises full state machine before any approved live adapter.

**P2 — External gated purchase pilot:** provider enrollment, named merchant support, terms/privacy disclosures, test/live credentials, issuer-action flows, receipts/refunds/support, tax/PCI/consumer-law allocation verified by responsible partners/counsel. Do not claim that merchant-of-record separation resolves every platform obligation.

**P3 — Expansion:** same-owner offline replica/failover only with authority fencing; explicit cross-owner shared scopes; additional merchant/network adapters, scheduled delegated purchases under expiring constrained mandates, advanced agent integrations.

Required tests: two clients approve same purchase; two brains attempt spending; crash immediately before/after provider acceptance; delayed/duplicate/out-of-order webhooks; expired quote and changed shipping/tax; reservation races; vault/ledger unavailable; order accepted but receipt delivery lost; capture failure; refund partial/fails; scope revocation and deletion behavior; mobile backgrounding; real glasses signal degradation. Also execute source→built bundle→clean installation→upgrade scenarios on supported hardware/OSes. Existing mocks are useful but cannot establish release readiness.

No guarantees of universal shopping, medical correctness, full offline continuity, or worldwide network/payment access can be inferred from the current code or public protocols.
