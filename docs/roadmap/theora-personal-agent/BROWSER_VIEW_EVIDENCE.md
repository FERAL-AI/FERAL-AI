# Native browser observation and computer-use integration

Updated October 4, 2026. This extends the existing FERAL browser controller and
native app. It does not introduce a second agent runtime or action authority.

## Behavior

The Mac app gains a **Browser** destination. A person explicitly starts viewing
the page already attached to FERAL. The viewer never initializes a browser,
switches tabs, navigates, types, clicks or grants task permission. Use Chat and
Oversight to control task execution. Stopping viewing retires pixels immediately
and requests revocation; it does not cancel the underlying task.

Frames are actual viewport JPEGs, held in memory, with a sequential maximum of
two refreshes per second. The image dimensions are bounded to 1280 pixels and
the encoded payload to 2 MiB. The input-location marker uses actual recorded
browser mouse coordinates, scaled to the returned image. Coordinate and CDP selector actions have markers; Playwright selector click/hover
currently do not. No marker is invented for a selector fill that does not move a pointer. This is a browser input marker,
not a claim that the operating system cursor moved.

The driver masks ordinary password controls and hides child frames during capture.
Unsupported website shadow-root pages refuse viewing. Other page content remains visible;
this is not comprehensive secret detection. Masking temporarily changes page
presentation and may interfere with interacting with masked controls. Stop viewing
before manually using those controls. This slice does not provide whole-desktop
screen sharing, interactive remote control or independent per-agent browsers.

## Headless contract

| Route | Contract |
|---|---|
| `GET /api/browser/view/targets` | Already attached target only; no browser launch or private title/URL metadata |
| `POST /api/browser/view/start` | Exact current target and strict boolean consent; returns an ephemeral viewing ID/token and expiry |
| `POST /api/browser/view/frame` | Exact ID/token; sequence, timestamp, real JPEG dimensions, optional input location; no returned token |
| `POST /api/browser/view/stop` | Exact ID/token revocation, including captures currently awaiting completion |

Viewing is limited to direct local operator requests with the native-view header;
proxied/remote callers and browser Origin or Sec-Fetch requests refuse. The header is a cross-site request fence, not
authentication. Existing application authentication remains in force. The
single-user local deployment trusts local processes; these routes do not claim
multi-user identity isolation. No new action endpoint is introduced.

Each lease binds the exact controller, attached target, page and CDP connection.
Expiry, target/connection replacement, explicit Stop and late-response checks
prevent continued frame delivery. Native origin/generation changes, leaving the
view and malformed/expired images clear local media. Tokens and images are not
stored in normal logs or recordings. An unconfirmed Stop is shown as such; the
grant still expires within five minutes. Viewing permission does not authorize
computer control or replace ToolRunner approval.

## Correct-field typing repair

The prior CDP fallback ignored the requested field and sent a key event, unlike
the Playwright field implementation. The repair resolves the current field,
refuses unresolved or ambiguous targets, checks focus, inserts Unicode text and
checks the result without returning inspected private field values. A dispatch
whose result cannot be verified is not automatically repeated.

## OpenAI integration boundary

OpenAI documents two distinct integrations. Its local computer-use interface lets
the application execute model requests through its own persistent environment
and return observations; existing function tools can remain in use. Its Agents
API browser runs in OpenAI infrastructure and exposes screenshot events. Neither
API installs ChatGPT's viewing interface into FERAL.
[Local computer-use guide](https://developers.openai.com/api/docs/guides/tools-computer-use),
[hosted browser guide](https://developers.openai.com/api/docs/guides/agents-api/tools/computer-use).

Those API facts were checked in current official documentation. No OpenAI paid
computer-use session or personal-account action was executed for this slice.
The local-first implementation retains FERAL's CDP/Playwright and AX/GUI tools.
FERAL already delivers browser/screen captures as image inputs to capable models
through its orchestrator, including OpenAI Responses image conversion. The
existing image-delivery suite passes 44 cases in an isolated profile; this is
wire/fixture verification, not a paid vision-model session. An optional structured
computer-use adapter can extend that foundation. New provider adapters must preserve the central tool approvals and return actual
observations with their call identities. Arbitrary generated scripts need a
separately reviewed sandbox contract before exposure to a personal computer.

## Verification and remaining gates

Source fixtures, disposable real-browser checks, compiled native checks and
actual packaged GUI acceptance are recorded separately in
[WORK_STATE](WORK_STATE.md). A unit/transport pass does not certify a merchant,
cloud account, payment, hardware device or release installer.

Frozen combined backend checks pass 1,230 with one opt-in real-Chrome case skipped.
The separate installed-Chrome run passes 26 focused cases, including the real
browser case. These counts overlap. Native Browser 74/Providers 50/Onboarding 52
assertions and linked model/desktop checks pass; production typecheck passes.
Full mypy remains 809 diagnostics in 233 files, unchanged from the baseline.
Packaged 9.37/source b9af now passes actual native consent, real masked frames,
intended-field Unicode, approved coordinate marker, Stop, navigation retirement,
expiry/error clearing and normal Quit. [Exact acceptance](NATIVE_9_37_ACCEPTANCE.md)
records synthetic task dispatch, approval-button limits and the existing
empty-session REST mismatch. The corrected committed Chrome harness at
`10d1223ba0328f33e06fab43cc04415c06313d07` passed all 11 checks in 5.19 seconds;
owned Chrome exited normally and its debug listener closed.

Remaining integration work: task-owned browser contexts and account-write leases;
one common foreground lease for AX/GUI desktop control; a consented desktop view;
chat-linked task progress and takeover; optional provider-specific structured computer-use
adapters; measured failure/latency acceptance on representative applications.
Continue the dependency cards in
[the multitasking plan](MULTITASKING_AND_EASY_SETUP_PLAN.md). Parallel browser work
must not reuse the current shared mutable page as if it were isolated.
