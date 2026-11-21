# Execution, testing and release plan

September 30, 2026. Ticket IDs are proposed planning identifiers, not issues already created in an external tracker. Effort estimates are relative; no date or staffing commitment is implied. Each milestone ends in reviewable evidence.

## Ownership and ordered work

Roles needed: product/design lead, core/backend owner, native iOS owner, desktop owner, test/release owner, hardware/firmware specialist and responsible privacy/payment/medical review partners as applicable. One person may fill multiple roles, but parallel code workers do not replace physical device tests, merchant onboarding or customer recruitment.

| Milestone | Work and responsible owners | Exit evidence |
|---|---|---|
| M0: establish the baseline | Product selects pilot job and recruits; iOS establishes reproducible tests/build; core specifies ownership/event/action contracts; release inventories dependencies/data flows | Clean tests, documented hardware matrix, shared fixtures, decision register and interview evidence |
| M1: one agent | Core implements owner isolation and durable conversation/task APIs; iOS migrates histories and completes HUP; desktop consumes same contracts | A/B account isolation; same turns/tasks across devices; replay/correction/deletion tests pass |
| M2: glasses pilot | iOS handles audio/BLE/background/outbox; core verifies real relay; desktop ships usable native integrations; product delivers memory/source controls and small avatar catalog | Real-device sessions, cellular/sleep recovery, measured battery and consent behavior; signed private-alpha builds |
| M3: useful outcomes | Product runs four-week pilot; integrations owner adds selected calendar/email-draft workflows; QA tests approvals and final evidence | Retention, paid continuation, verified outcomes and nuisance/error measures reported with cohort uncertainty |
| M4: commerce sandbox | Core implements deterministic purchases; iOS supplies trusted review/payment handoff; payments owner secures named provider/merchant access | Simulator and provider sandbox races/crashes/unknowns/refunds pass; no real spending required |
| M5: constrained live release | Release signs/packages/updater; support/operations prepared; product verifies geography/claims/licenses; payments owner approves merchant pilot | Clean installs/upgrades, restore and incident drills, licensed OSS release, live-supported capability list |
| M6: expansion | Additional integrations, hosted capacity, sensors and delegated mandates based on demand | Separate scoped approvals and measured value for each extension |

Product discovery can run alongside M0–M2. Shopping discovery/handoff can precede full transaction completion. Build M4 when pilot evidence justifies it; do not make comprehensive commerce a prerequisite for learning whether memory/follow-through is valuable.

## Core and desktop backlog

| ID | Priority and scope | Dependency | Acceptance |
|---|---|---|---|
| CORE-01 | P0 owner/device lifecycle and account-backed/local-owner enrollment | Product ownership decision | Cross-owner access fails; revoke/unlink blocks new submissions and safely cancels uncommitted work; already submitted/unknown outcomes reconcile under restricted authority; recovery tested |
| CORE-02 | P0 event/conversation contract and persistent outbox ACKs | CORE-01 | Python/Swift/TS fixtures agree; duplicate/reordered events and version negotiation work |
| CORE-03 | P0 inspectable memory, source lineage, corrections/deletion | CORE-02 | Sources resolve; corrected facts supersede inference; reconnect/restore cannot resurrect deleted content |
| CORE-04 | P0 durable exact-action approvals and executor leases | CORE-01/02 | Approval bound to immutable effect; expiry/revoke/races/cancel tested; every executor has enforcement |
| CORE-05 | P1 health provenance and transactional ingestion | Hardware manifest + CORE-02 | Missing/stale/estimated data labelled; dedupe/partial batch/rollback concern reproduced and resolved |
| NET-01 | P0 real authenticated relay and availability model | Deployment/operator choice | LAN/cellular/NAT/TLS/sleep/revoke tests against actual edge, including untrusted-listener auth |
| VOICE-01 | P0 complete audio/outcome fixtures and tool ownership | CORE-02 | Fallback encoding/rate, cancellation, denied/expired outcomes, reconnect binding and duplicate tools tested |
| DESK-01 | P1 direct consumer UI and native adapter wiring | CORE-02/04 | Shortcut reaches actual voice flow; accessible conversations/inbox; no orphan process on quit |
| DESK-02 | P1 install/sign/update/rollback | Supported OS/CPU matrix | Fresh Mac/Linux machine needs no repo/Python; signed update verified, tampering denied, interrupted migration recovers |
| PAY-01 | P0 before live spend: quote and purchase ledger | CORE-04 + provider interface | All state-machine transitions tested; failed ledger reads/writes deny spend; concurrent reservations enforce caps |
| PAY-02 | P1 named merchant sandbox and trusted checkout | PAY-01 + external access | Signed webhooks/dedupe, issuer action, unknown outcome, receipt, cancellation and partial refunds verified |
| OSS-01 | P1 dependency/asset/hardware rights and clean-clone release | Vendor cooperation | License manifest, software-only tests, legal SDK installation path, no private credentials needed |

Related existing source areas are named in [core research](CORE_COMMERCE_RESEARCH.md). Avoid rewriting the giant orchestrator/server simply to make a cleaner diagram. Introduce interfaces and move responsibility when a tested vertical slice requires it. Measure redundant responsibilities before removing implementations.

## iOS first assignment

Start with IOS-01, IOS-02 and a joint contract review for IOS-03/04/05 from [the native handoff](IOS_AGENT_HANDOFF.md). Use hardware-free Swift fixtures while licensing/build dependencies are resolved. Core must publish the corresponding schemas/semantics; iOS cannot solve canonical identity, action idempotency or cross-device spending alone.

Then deliver voice and BLE recovery, privacy and approval inbox, followed by APNs/App Intents and the initial avatar. Keep current iOS 16 deployment unless product explicitly chooses a change; newer speech/model APIs are optional availability-gated adapters. Source concerns should become reproduced failing cases, not speculative patches.

Handoff outputs from the two teams: versioned fixture corpus; owner/brain/device model; migration plan from both histories; error/terminal outcomes; audio formats; action approval and checkout terms; background behavior table; consent matrix; supported hardware/OS matrix; real-device logs with sensitive content redacted. Each contract change includes both clients' compatibility plan.

## Validation matrix

| Layer | Required cases and evidence |
|---|---|
| Contract | Every frame/event/outcome, duplicate IDs, clock skew, unknown version, incompatible capability and batch rejections |
| Identity | User A/B switching, pair/unlink/revoke, key rotation, lost device, deleted account, backup restore and locked storage |
| Memory | Temporal recall, source attribution, contradictions, user correction, abstention, consent withdrawal and deletion through derived records |
| Audio | Real glasses microphone, AirPods/handset route changes, interruption/call, barge-in, missing models, fallback formats and long sessions |
| Sensing | Device capability differences, worn state, missing/stale signals, heuristic quality versus calibrated uncertainty, calibration metadata, history backlog and duplicate/partial ingestion |
| Network | Off-LAN real relay, NAT/captive portal, suspend/force-quit/eviction distinction, sleep/wake, TLS expiry, outages and backlog limits |
| Actions | Competing devices, expired/revoked approval, changed arguments, executor crash, delegated permission limits and malicious tool output |
| Payments | Quote tax/shipping mutation, double tap, ledger outage, before/after-commit crash, unknown outcome, reordered webhooks, issuer action, refunds and receipts |
| Distribution | Clean clone, clean install, signed/notarized Mac, supported Linux variants, no external Python, safe upgrade/data migration and rollback |
| Privacy/security | Consent-denied traffic capture, participant refusal/stop/session-derived deletion, tenant isolation, secret redaction, permission escalation, prompt injection, OAuth/webhook binding and restore/delete drills |
| Product | First-time pairing, time to first benefit, repeated outcomes, wear time, cleanup/nuisance burden, paid continuation and support load |

Native vendor SDK/device tests cannot be replaced with simulator mocks. Mock tests cannot establish merchant acceptance or relay readiness. Clinical validation cannot be replaced with unit tests or an LLM evaluation. Record unsupported scenarios explicitly.

Use deterministic tests for policy/state transitions, property tests for ordering/deduplication and integration/chaos tests for crash boundaries. Use human-labelled evals for memory and tool plans. [OpenAI agent evals](https://developers.openai.com/api/docs/guides/agent-evals) describe reproducible workflow evaluation; preserve privacy by using synthetic or consented/redacted traces. Release gates must include critical-case pass/fail, not only aggregate averages.

## Proposed quality targets

Set measured baselines first. Suggested pilot gates are at least 90% precision for surfaced commitments, zero observed material unauthorized actions, at least 60% week-four retention and at least 50% choosing honestly priced continuation in the recruited cohort. These are internal hypotheses, not industry benchmarks or proof of zero risk. Report denominators, uncertainty, recall and task complexity.

Track p50/p95 voice response, interruption latency, transcription error/coverage by device/language/noise, first-pair success, recovery time, duplicate events, grounded-memory accuracy, completed actions, battery drain and resource cost. Set latency SLOs after real hardware measurements rather than publishing unmeasured promises. Severe authorization failures block release regardless of retention or average accuracy.

## Cost and operational design

Model unit economics before choosing always-on hosted voice. Cost per active user includes audio input/output duration, model tokens, summarization/embeddings, storage, relay/push, executor compute, payment fees, support and hardware warranty burden. Estimate low/median/high usage from the pilot, then apply price tests and spending quotas. Keep company credits and the user's purchasing budget separate.

Operations need encrypted backup/restore, service health and reconnect metrics without raw health/transcript logs, redacted error reports, per-owner rate/storage limits, scoped feature flags, provider degradation fallbacks, credential rotation and incident response. Merchant failures need human support, unresolved-order visibility and escalation procedures. Support cannot resolve unknown payments by pressing buy again.

Publish a supported-feature matrix by deployment mode, device, OS and geography. Account for laptop-asleep availability explicitly. A hosted service must be tested for cross-owner isolation and capacity, while self-hosting must have documented recovery/upgrade/export procedures. Signing/update keys are protected release infrastructure, not repository constants.

## Open-source boundary

Inventory licenses before publication: existing core, web/desktop, copied source, vendor frameworks, model weights, datasets, fonts and avatars. Preserve existing license obligations; do not assume the umbrella project license covers all components. Open-source payment adapters do not distribute merchant/provider credentials or guarantee access to private programs.

Separate redistributable software from legally obtained hardware SDK drops. Provide hardware interfaces, fixtures and a simulator/replay mode so outsiders can build/test useful software without proprietary binaries. Reconcile the missing iOS gitlink acquisition path and actual Xcode project. Document cloud dependencies and expose a genuinely operable self-hosted path for features advertised as self-hosted.

Release artifacts should include versioned contracts, dependency/license manifest, architecture/data-flow docs, threat model, redacted examples, contribution instructions and reproducible CI checks. Provide security reporting, migration notes and signed releases. Do not publish secrets, customer health data or private conversation fixtures. This plan does not publish or change any repository license.

## Decisions and external dependencies

| Decision | Recommended planning default | Evidence needed before locking it |
|---|---|---|
| Initial customer/job | Adult frequent-conversation glasses wearer; commitments/follow-through | Interviews, paid pilot, comparison to actual workaround |
| Brand | Theora product with FERAL engine, pending preference | User/product naming decision |
| Brain availability | Optional isolated hosted + supported self-hosting | Demand, privacy/hosting decisions and unit economics |
| Launch geography/claims | Narrow US-first wellbeing pilot, provisional | Actual countries, intended use, responsible legal/medical review |
| OS/device matrix | Current supported iOS plus selected Mac/Linux configurations | Vendor compatibility and measured release capacity |
| First connectors | Notes/tasks, selected calendar/email drafts | Pilot job demand, OAuth/provider access and policy tests |
| First commerce | Search + trusted checkout handoff, named merchant later | PSP enrollment, merchant agreement, sandbox/live approval |
| Avatar runtime | Small licensed catalog; benchmark renderer | Performance, art rights and native/web accessibility |
| Health sensing expansion | Separate future raw-signal program | Firmware access, hardware quality and appropriate validation |
| Retention/training | Minimal configurable retention; no cross-user training by default | Purpose, consent, cost and deletion/restore design |

Unresolved references: exact Instinct product; Limitless current official availability; Dot closure year. Recheck competitor availability and all platform/payment terms before implementation/release because this research is dated September 30, 2026.
