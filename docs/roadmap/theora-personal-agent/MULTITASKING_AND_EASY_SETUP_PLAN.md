# Multitasking, voice, provider access and easy installation

Research and source audit: October 4, 2026, against source `9a40b9ac8`.
This extends the existing FERAL completion plan. It does not create a new runtime,
memory store or payment authority. New wire fields, routes and adapters below are
proposals until implemented, negotiated and tested. Existing functionality stays
available while these additions gain acceptance.

## Decisions and current boundary

| Requirement | Decision | Confirmed now | Remaining work |
|---|---|---|---|
| Talk while several tasks run | A conversational lane delegates to separately owned durable jobs | TaskFlow storage, tracked receipts, parallel workers and managed chained voice exist | Durable job ownership independent of voice socket, bounded concurrent scheduler, conversational delegation and result fanout |
| Subscription / API / local choice | Separate supported authentication and capability paths | Shared provider catalog/config; a new official ChatGPT plan-usage path is documented | FERAL registration/provider adapter, account acceptance; Claude SDK eligibility clarification; install UI |
| Browser / computer / coding together | Task-owned browser contexts and coding workspaces; one foreground desktop owner | CDP/Playwright, AX/GUI primitives and managed ACP are implemented | Shared browser currently has one active page; common foreground resource lease and live preview need integration |
| Local reasoning, vision and voice | Reviewed optional runtime/model packages with measured capabilities | Ollama adapter, vision paths, whisper.cpp/faster-whisper and Piper/macOS speech options exist | Runtime installation, safe downloads, hardware/context preflight and real acoustic/resource benchmarks |
| iMessage shopping choices | Evidence-backed offers, text/image fallback, exact final-total review | Channel abstractions, Messages send, money policy and purchase preview exist | Receive adapter, task-owned checkout, Link adapter and merchant outcome verification |
| Two–three-step setup | Choose mode → connect/install → verify and chat | CLI shortcut and native single reviewed provider stage exist | Packaged parity acceptance; installer and first-inference verification |
| Phone/glasses continuity and proactivity | Same owner/task authority, exclusive audio owner and explicit opt-ins | HUP frames/audio, checkpoints, sync scopes and proactive engine exist | Correlated capture, mobile job/audio handoff, event-driven delivery and device acceptance |

Source inspection is evidence of code presence and limits. Current-candidate
actual GUI evidence is in [9.36 acceptance](NATIVE_9_36_ACCEPTANCE.md). The
three research audits did not perform cloud logins, downloads, physical audio,
Messages, payment or glasses tests. The disposable CLI provider-switch
reproduction was executed; it retained the preceding endpoint and fallback.
The subsequent repairs have [separate source evidence](SETUP_PARITY_EVIDENCE.md)
and remain outside 9.36.

## 1. Independent jobs while voice stays conversational

**Verdict: reuse TaskFlow, and separate a task's lifetime from its voice connection.**
`agents/chat_turns.py` has durable receipts but unfinished interactive work is
cancelled after owner socket detachment. `agents/taskflow.py` has persisted steps,
dedicated task sessions and conservative interrupted-effect reconciliation, but
its scheduler currently awaits one flow at a time. Parallel workers inside a
turn are not independently durable jobs. Current managed chained voice admits
one explicit utterance, completes its task and then speaks; it is not continuous
conversational multitasking.

Extend these existing components with explicit promotion from a conversational
request to a durable job. Record authenticated owner/profile, dedicated task SID,
originating conversation/request/turn, task revision, resource requirements,
policy/budget snapshot and each operation attempt. Commit state and ordered
progress/result/approval events before publishing them. Devices subscribe with a
cursor; reconnection reads status instead of resubmitting work. The parent
conversation's existing checkpoint writer serializes result insertion.

Use a bounded concurrent TaskFlow scheduler and existing supervisor. Prioritize
capture/playback and foreground conversation over background vision/coding.
Independent jobs can proceed together; shared carts, accounts, files and devices
need resource ownership. Limit worker depth/count, wall time, tool calls, queued
work and combined usage. A user can change these limits; lack of a limit must
not imply infinite agent recursion.

Expose distinct controls: **stop speaking**, **mute microphone**, **cancel this
task**, **pause automation**, and **end call**. Speech interruption need not
abort a background job. Cancelling a worker cannot undo an order already sent;
report its known/unknown outcome and review any external cancellation separately.
Logout, profile replacement and revoked authority still fence jobs. Do not remove
interactive detach cancellation globally to obtain background survival.

Acceptance: two jobs plus conversation; cancel A without stopping B; revise A
while old results arrive; disconnect/reconnect; crash before/after an effect;
exact review isolation; durable result delivery without effect replay or replayed
speech. Show received/running/needs approval/completed/outcome unknown separately
from delivered/read/played acknowledgments.

## 2. Subscription, API and local models

**Verdict: offer three honest connection modes, with capability-specific credentials.**

OpenAI now documents an eligible open-source/local ChatGPT plan-usage flow.
Initial dynamic registration needs no client secret or partner key; the app must
validate the grant, preserve host/account identity and protect renewable tokens.
Actual FERAL/account eligibility is untested. Paid or remotely hosted distribution
has a separate access process. [Official overview](https://developers.openai.com/siwc/token-sharing-open-source),
[registration](https://developers.openai.com/siwc/token-sharing-open-source/sign-in).

Use a dedicated provider adapter and account-specific model catalog. The public
Responses route requires streaming with server storage disabled and different
input/tool encoding. Its current preview excludes audio/transcription and hosted
computer-use/MCP tools and several ordinary request fields, including
`max_output_tokens`; FERAL must execute supported local tools itself. Do not pass
OAuth tokens into the existing generic adapter or advertise voice inclusion.
[Inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference),
[preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations).

FERAL's current `providers/codex_provider.py` uses an existing Codex login/app-server
and drops FERAL tools. It is not this new registration flow or proof of central
tool parity. The documented app-server plan integration is another supported
adapter choice; map tool requests back to FERAL authorization before enabling
general execution. Do not duplicate or scrape another application's tokens.
[Plan app-server configuration](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server).

Anthropic's current SDK guidance says Agent SDK/`claude -p` usage can draw plan
limits, while its account policy directs developers building for others to
API/cloud credentials and makes third-party subscription use discretionary.
Keep both constraints visible. FERAL's direct Anthropic adapter uses API keys;
an approved SDK delegation path needs its own eligibility, billing and policy
mapping. Do not ship an unrestricted subscription toggle based on copied OAuth
credentials. [SDK plan guidance](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan),
[account policy](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account).

API providers remain explicit usage-based connections. Local mode uses genuinely
local installed models; an Ollama `:cloud` tag is a cloud choice. No silent cloud
fallback for local privacy mode. Separate reasoning, vision, transcription,
speech and embeddings choices in advanced settings, with tested capability
readback and one shared catalog/config contract across CLI, web and native.

Current official model research includes GPT-6 Astra, GPT-6.1 Sol and GPT-6 Luna,
plus the voice models below. Availability differs by account and connection mode.
Discover entitled models instead of treating a static list as access proof.
Request templates and capabilities must be tested before enabling a model; names
alone are not integration. Compare quality, latency and actual cost on FERAL tasks
and choose the lightest model that meets the agreed bar.
[Current model guidance](https://developers.openai.com/api/docs/guides/latest-model).

## 3. Installation and setup that fits three steps

**Verdict: bundle the app runtime, then guide optional model installation inside setup.**
The current bundle includes Python, FERAL core and pinned OpenCode. It does not
include Ollama, Codex, Claude Code or model weights. Terminal PATH success does
not prove a GUI can find a runtime. The existing native preview also deliberately
uses a separate default profile from CLI; switching clients is not migration.

1. **Choose:** ChatGPT plan, an API provider, or local. Show which features that
   connection supports and how it is billed. Identity/avatar choice can stay
   compact; permissions and optional integrations do not become mandatory pages.
2. **Connect or install:** browser sign-in, protected API key entry, or reviewed
   local runtime/model download. Detect an existing compatible runtime first.
   Show source/version, model license, download/disk size, memory/context needs,
   destination and local/cloud behavior. Do not replace a user's daemon silently.
3. **Verify and chat:** confirm runtime ownership/version and model capabilities,
   then run an explicitly authorized harmless inference through FERAL and check
   its durable terminal receipt. Test tools/vision/voice separately before those
   badges become ready. A saved setting or probe alone is not working chat.

The backend operation should own bounded download/install progress, cancellation,
partial-file cleanup/resume and atomic activation. Reuse caches and installed
weights, avoid duplicate profiles/downloads, and show actionable permission,
capacity and network errors. Model removal previews exact owned files and keeps
shared user models. Declining a download leaves an inspectable incomplete state.
Signed dependency provenance and clean-machine tests are required for unattended
installation; a bundled Python audit does not close that gate.

Current Ollama requires macOS 14 or later, whereas FERAL currently supports
macOS 13. Gate this option by actual OS/hardware rather than failing at first
chat. Do not promote stale wizard model tags: its `llama3.3:8b` suggestion is not
in current official Llama 3.3 tags. [Ollama macOS](https://docs.ollama.com/macos),
[model tags](https://ollama.com/library/llama3.3/tags).

Confirmed setup repairs: backend model IDs are strings while two native readers
expect object rows; CLI `models set` can retain the old endpoint/fallback when
changing provider. Preserve manual IDs and same-provider custom endpoints;
changing mode needs explicit compatible endpoint and fallback/privacy review.
Add contract tests using real route shapes and real saved-config reloads, then
test CLI, web and native against one explicitly selected profile. Preserve and
review migration from existing profiles separately.

## 4. Voice on Mac, phone and glasses

**Verdict: GPT-Live is the documented cloud fit for conversation during delegated work; local voice needs its own measured pipeline.**
`gpt-live-1` provides full-duplex conversation and client delegation to an
application-operated backend. FERAL should own task execution/memory/approvals
and send short verified progress/results to the voice frontend. Live has no
direct image/video input; a selected glasses frame goes to FERAL's vision backend.
This is docs confirmation, not account or acoustic acceptance.
[Live guide](https://developers.openai.com/api/docs/guides/live),
[delegation](https://developers.openai.com/api/docs/guides/live-delegation).

Live has a different transport/event lifecycle from Realtime. Implement a
negotiated adapter rather than replacing a model string. Bind delegation ID to
the durable task/request/revision and use transcript fragments plus task state;
the delegation event itself does not contain the task text. Playback state comes
from the actual audio player. Close/reconnect and delayed results must preserve
task authority and avoid replaying old speech.
[Live migration](https://developers.openai.com/api/docs/guides/live-migration).

Official Live pricing is $0.05 per session minute, billed per second, plus backend
and tool costs. An active hour therefore costs $3 for voice duration alone.
Silence and waiting still count; microphone mute does not close billing. Display
voice-duration and model/task allowances separately, close idle paid sessions
according to an explicit setting, and reconcile final usage.
[Model pricing](https://developers.openai.com/api/docs/models/gpt-live-1),
[voice accounting](https://developers.openai.com/api/docs/guides/voice-latency-cost).

Current Realtime alternatives are `gpt-realtime-2.1` and mini. Existing FERAL
defaults reference older models scheduled for January 20, 2027 removal; migrate
with protocol/account/quality tests and preserve explicit user settings.
Realtime supports its own VAD/interruption controls; do not copy these fields
into Live. [Realtime model](https://developers.openai.com/api/docs/models/gpt-realtime-2.1),
[mini](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini),
[deprecations](https://developers.openai.com/api/docs/deprecations).

Use one audio owner per conversation, with a reviewed generation-fenced handoff
between Mac and phone; phone transport can relay the selected glasses mic/output.
Drain/revoke the old route before admitting the new one. Handle incoming calls,
Bluetooth changes, unplugged devices and denied permissions. Late old audio or
camera frames cannot enter the new task. iOS background/suspension behavior needs
real device acceptance; no perpetual microphone guarantee follows from HUP.

Local mode can combine existing whisper.cpp/faster-whisper, local reasoning and
vision, and local speech output. Benchmark that stack rather than claim equal
full-duplex quality. The native audio implementation does not currently enable
voice processing; speaker echo cancellation and noisy-cafe interruption remain
open. Review Piper engine/voice redistribution licenses before bundling.
[whisper.cpp](https://github.com/ggml-org/whisper.cpp),
[Piper](https://github.com/OHF-Voice/piper1-gpl),
[Ollama vision](https://docs.ollama.com/capabilities/vision),
[Apple voice processing](https://developer.apple.com/videos/play/wwdc2019/510/).

Audio acceptance: headphones then speakers, cafe/TV/traffic/keyboard noise,
non-wearer speech, echo versus genuine interruption, long pauses, route changes,
mic denial and local-model load. Measure first useful speech, P50/P95 response,
missed/false interruptions and capture/playback lag while two jobs run.

## 5. Reliable computer use, coding and shared memory

**Verdict: keep the existing executor and add explicit resource ownership before parallel control.**
FERAL's `skills/impl/browser_use.py` is its own CDP/Playwright controller, not the
upstream Browser Use package. Keep it as the executor; evaluate another planner
only behind the existing ToolRunner and policy gates. Isolate independent tasks
with task/account-owned browser contexts, page target and document revision.
DOM/Accessibility observations should drive actions, with screenshots for needed
visual context and pixel fallback. Batch small verified action sequences where
safe, re-observe after navigation or an uncertain state, and verify postconditions.
[Playwright contexts](https://playwright.dev/python/docs/browser-contexts),
[Browser Use](https://github.com/browser-use/browser-use).

The existing browser shares one active page. Add context/target leases; reject
stale refs, wrong accounts and user-interfered navigation. A common foreground
desktop lease is also needed across AX/GUI/browser OS dialogs. Rate limiting is
not ownership. Many isolated browser/coding jobs can run, but two workers cannot
independently seize the same physical desktop focus. Queue/handoff must be visible.

Keep managed OpenCode ACP and trusted session/workspace grants. Separate coding
workspaces prevent conflicting edits; common files need serialization/review.
Opt-in Codex/Claude/OpenCode CLI adapters or hooks can publish scoped activity
with agent/task/workspace provenance, redaction and retention controls. Existing
ACP tracking does not observe every unrelated terminal or provide complete
historical reasoning. [OpenCode ACP](https://opencode.ai/docs/acp/),
[server sessions](https://opencode.ai/docs/server/).

Live screen preview needs explicit capture consent, selected target, pause/stop,
bounded frame rate and sensitive-field masking. A user's ordinary Mac, a browser
context, changed environment variables and a Git worktree are not an OS security
sandbox. Offer a tested fail-closed container/VM/dedicated desktop lane where
containment is needed; describe the permissions of host execution accurately.

Reuse working memory, checkpoints, episodes/wiki and scoped sync. Deliberately
stored personal facts can inform several same-owner tasks without merging all
private transcripts. Cross-person projects/social exchange require membership,
record-level grants and revocation. Revocation stops future sharing; it cannot
erase a copy already received by another peer. Keep health/camera/audio/payment secrets
private by default; provenance, correction, deletion and retrieval scope remain
visible. No hidden recording of all CLI activity or all life to fill memory.

Measure action success, verified task success, wrong-target events, observation
latency, tool round trips, budget/resource pressure and recovery. No claim to be
the fastest or best harness follows from having many workers.

## 6. iMessage product choices and purchasing

**Verdict: implement a receive-capable channel and three verified offers, then separate purchase approval and completion.**
Use version-gated basic `imsg` transport with text/files, or an already installed
BlueBubbles server. Keep SIP enabled. Advanced injected Messages features are
private and unstable; they are not an installation requirement. A relay sends
as its signed-in Messages identity, so enrolling a dedicated identity or bounded
self-thread is a product decision. Full Disk Access/automation permission,
recipient/thread allowlists, GUID deduplication, cursor recovery and echo/history
filtering are necessary. No current receive-capable adapter was found.
[imsg](https://imsg.sh/),
[BlueBubbles webhooks](https://docs.bluebubbles.app/server/developer-guides/rest-api-and-webhooks).

Native/web use a dedicated typed shopping component; broad Gen-UI remains
deferred. Basic iMessage receives a product contact sheet, numbered summaries
and detail links. Arbitrary interactive bubbles require a separate supported
iOS Messages extension with fallback, not Mac AppleScript.
[Apple Messages layout](https://developer.apple.com/documentation/messages/msmessagelivelayout).

For an identified perfume, show up to three genuinely observed offers. Bind each
offer ID/revision to exact product/variant/quantity, merchant/domain/URL, source
image, currency/unit price, known shipping/tax/fees, final total or **unknown**,
stock observation, fulfillment and evidence timestamp/expiry. Label delivery
or lowest-total comparisons only where evidence supports them. Selecting option
2 prepares checkout; it does not spend money. Do not generate bottle images and
present them as retailer evidence.

Confirm the final all-inclusive total, item/quantity, merchant, destination,
payment method and relevant recurring/cancellation terms. Bind one-use approval
to authenticated owner/task, exact quote hash/revision and operation attempt.
Recheck terms/caps/account immediately before submission; changes renew review.
Bare yes or a forwarded card cannot resolve a different pending purchase.

Use a narrow Link adapter with per-user wallet credentials outside model context
and transcripts. The current documented wallet is available to US/Canadian
consumers without a Stripe business account; actual eligibility and test-mode
flows remain untested here. Credential creation/wallet approval is separate from
merchant order confirmation. Record dispatch before the effect and reconcile an
unknown outcome rather than buy again. Reserve concurrent spending allowance so
two purchases cannot race a cap. [Link CLI](https://github.com/stripe/link-cli),
[wallet availability](https://docs.stripe.com/agentic-commerce/agents/link-agent-wallet).

Existing `web_actions.py` returns purchase previews, not completed orders. Reuse
`security/commerce.py`, exact approval and TaskFlow attempt storage; raw browser
HTTP actions are not the new checkout authority. Test denial, changed totals,
expiry, absent evidence, duplicate/foreign replies, card-data masking, lost ACK
and merchant reconciliation before an authorized real purchase.

## 7. Glasses trigger, proactive operation and iOS handoff

**Verdict: bind a contemporaneous authorized frame and utterance to one task; run proactive work under independent policies and budgets.**
HUP and the glasses buffer already carry bounded frames with provenance/time.
Current generic attachment chooses the freshest frame across devices; that is
insufficient for “get me that” with more than one device or owner. Capture/request
a frame correlated to selected owner, node/device, utterance, timestamp and task;
reject stale, foreign and ambiguous attachments. Continue the same job in the
enrolled Messages thread, with image/product confirmation when identification
is uncertain. Physical capture and voice remain device gates.

The proactive engine already has switches/cooldowns and cost guards. Prefer
events from changed calendars, sensors, jobs and opted-in CLI activity; run cheap
freshness/relevance/cooldown checks before model inference. Coalesce duplicates,
set quiet hours and allowed actions, show why a suggestion appeared, and let
users correct memory or disable a rule. Learning, proactive decisions, camera,
ambient recording and notifications are separate choices. No paid voice session
or continuous vision inference is required while idle.

A sleeping/offline Mac cannot execute local browser tasks. Show queued/paused/
unavailable truthfully; an explicitly selected always-on host is an option.
Local wake-up capture does not imply the reasoning model is continuously resident
or free. Phone offline queues bounded capture/job requests and reconciles later;
never replay an uncertain purchase. Give the iOS agent negotiated schemas and
fixtures for job subscriptions, audio ownership, correlated capture, approvals,
offline outbox and optional Messages extension. See [iOS handoff](IOS_AGENT_HANDOFF.md).

## Dependency order and acceptance cards

| Card | Work / ownership boundary | Completion gate |
|---|---|---|
| SETUP-01 | Existing native model readers + CLI switch, exclusive worker files | Real route DTO and saved reload parity; actual selected local model in native picker |
| MAC-01 | Parent integrates reliability source and assembled app | Disabled learning has no automatic calls; exact Stop/status, latency instrumentation and normal lifecycle |
| INSTALL-01 | Local runtime operation service + installer UI; parent wires shared routes | Cancel/resume/capacity/permissions, model identity/capabilities, committed first inference, clean Mac install |
| AUTH-01 | ChatGPT plan auth/provider; parent reviews catalog/vault/tools contract | Callback/scope/refresh/account-switch/usage-limit fixtures, actual consenting eligible account |
| TASK-01 | TaskFlow bounded scheduling + durable event subscriptions | Two jobs + conversation, isolated cancel/revision, reconnect/crash/unknown-effect reconciliation |
| RESOURCE-01 | Task browser targets + common desktop leases | Parallel contexts, stale/user interference, one foreground owner and verified actions |
| VOICE-02 | Live adapter + audio routing; parent owns authority/schema | Full-duplex delegation, audio controls, speaker/headset/noise tests under two jobs and budgets |
| MSG-01 | Enrolled receive channel + text/image offer delivery | Duplicate/echo/history/foreign/stale/uncertain-send fixtures, authorized Mac/phone identity |
| BUY-01 | Link adapter + quotes/attempts/spend reservations | Denial/change/expiry/no secret leak/no replay, authorized provider test mode and merchant receipt |
| IOS-02 | Separate iOS agent implements negotiated job/audio/capture handoff | Signed phone/glasses route, stale frame/device, offline/locked-phone and audio ownership tests |
| PROACTIVE-02 | Event-driven rules using existing settings/budget/memory | Disabled means no calls; cooldown/quiet hours, scoped learning and revocable actions |
| CODE-02 | Opt-in standalone CLI adapters and shared workspace activity | Provenance/redaction, exact task/project authority; no global process/history scraping |
| RELEASE-01 | Parent packages accepted Mac source and documentation | Backup/migration/rollback, clean install, signing/notarization/update and developer extension smoke |

Execute independent cards with exclusive files and one parent integrator. Shared
contracts integrate sequentially; freeze source for checks and packaging. Keep
actual account/device/merchant gates separate from fixtures. Mac remains first;
Linux expansion and Gen-UI remain deferred. Previous social, avatar, health,
memory, developer-platform and release work stays in
[request coverage](REQUEST_COVERAGE.md) and [release readiness](RELEASE_READINESS.md).
