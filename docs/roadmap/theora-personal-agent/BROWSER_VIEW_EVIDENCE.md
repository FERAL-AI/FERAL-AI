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
browser mouse coordinates, scaled to the returned image. Coordinate and CDP
selector actions have markers. The current integration tree also observes trusted
Playwright selector click/hover events; this later repair is outside immutable
9.37/source b9af. No marker is invented for selector fill or missing input
observations. This is a browser input marker, not a claim that the operating
system cursor moved or a requested task succeeded.

The driver masks ordinary password controls and hides child frames during capture.
Unsupported website shadow-root pages refuse viewing. Other page content remains visible;
this is not comprehensive secret detection. Masking temporarily changes page
presentation and may interfere with interacting with masked controls. Stop viewing
before manually using those controls. This slice does not provide whole-desktop
screen sharing, interactive remote control or independent per-agent browsers.

## Installed Chrome and existing-profile connection

Source inspection confirms that `BrowserController._auto_launch_chrome` uses the
installed Mac Chrome binary with a persistent FERAL-owned `chrome-profile` under
FERAL home. It does not inherit the personal default profile or its logins. The
controller can attach to an existing configured CDP endpoint. Actual acceptance
used disposable profiles; personal-profile access has not been tested.

Current primary documentation confirms two possible existing-profile bridges:

- Chrome DevTools MCP `--autoConnect` supports Chrome 144+ through the user's
  `chrome://inspect/#remote-debugging` opt-in and Chrome permission dialog. The
  documented connection chooses Chrome's default profile and can access all its
  open windows; page routing alone is not a FERAL authorization boundary.
  [Official connection guide](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md#connecting-to-a-running-chrome-instance).
- An extension using `chrome.debugger` can address tabs by ID with supported CDP
  domains. The current FERAL extension exposes chat and page context and has no
  debugger permission/control transport. Reusing it would require an implemented
  bridge and actual acceptance, rather than a manifest-only permission change.
  [Chrome debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger).

The old remote-debugging-port approach requires a non-default data directory on
Chrome 136+; it is distinct from the newer permission-based autoConnect path.
[Chrome profile restriction](https://developer.chrome.com/blog/remote-debugging-port).

The current integration implements direct discovery and selected-session CDP
connection to existing Chrome. It reuses the documented endpoint discovery;
it does not install or run Chrome DevTools MCP. Strict local consent precedes
bounded in-memory title labels, and each selection rotates the connection
identity owned by the selected chat. Existing task policy/approval/executor
checks capture that exact session, connection and target. Unsupported global
operations refuse without fallback. Disconnect closes FERAL's sockets, restores
its captured backing and never closes Chrome or clears its cookies.
[Exact implementation and acceptance limits](EXISTING_CHROME_PLAN.md).

The existing-profile permission flow and personal account outcomes remain
documentation/release gates. Controlled transport/route/native fixtures do not
certify them. Immutable 9.37 acceptance used a disposable independently started
Chrome profile; it does not contain or validate this later connection adapter.

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

## Observed Playwright selector markers

The selector click/hover repair retains Playwright's original dispatch methods,
selectors and five-second action timeout. A temporary closure observes trusted
click or mousemove events on the resolved element and records their actual
viewport coordinates. It does not guess a bounding-box center. Observation
failure preserves action behavior with no marker. Instrumentation has bounded
waits and cleanup, plus an expiring listener for an uncertain installation reply.

Markers bind the same Playwright page, selected target, CDP connection and both
Document and document-element backend IDs. Actual Chrome testing showed that
`document.open()` can replace HTML while retaining the Document ID; the stronger
fence retires those markers. Fill, failed/cancelled actions, navigation, rebind
and crossed observation revisions discard stale markers. Child-frame pointer
observations are unsupported. Independent trusted user input on the same element
can also be observed during this window: a marker is not proof of exclusive
agent-origin input and never authorizes an action.

Five focused suites passed **402 cases**, with three opt-in cases skipped. A
separate explicit installed-Chrome run passed the selector case: an offscreen
button scrolled into view, moved after listener installation, received real
trusted click/hover events and returned matching screenshot coordinates. The
changed button pixels, fill without a marker, failed selector and document
replacement were checked. Receipt:
`/private/tmp/feral-playwright-pointer-real-9x7y0cwv/receipt.json`; log:
`/private/tmp/feral-playwright-pointer-real-9x7y0cwv/pytest.log`.
Both source hashes remained unchanged; the owned runner exited normally, no
forced kill was required and the debug listener closed. The initial real
failure at `/private/tmp/feral-playwright-pointer-real-0cbkzdj1/` is retained.
Ruff passes; narrow typing retains six existing diagnostics, not a clean typing
claim. This is controlled browser/input acceptance, not packaged native or account
acceptance.

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

Historical frozen 9.37 integration checks pass 1,230 with one opt-in real-Chrome case skipped.
The separate installed-Chrome run passes 26 focused cases, including the real
browser case. These counts overlap. Native Browser 74/Providers 50/Onboarding 52
assertions and linked model/desktop checks pass; production typecheck passes.
That historical integration's full mypy reported 809 diagnostics in 233 files,
unchanged from its baseline. These results do not certify the later
existing-Chrome integration; its current evidence is recorded separately in
[the existing-Chrome contract](EXISTING_CHROME_PLAN.md).
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
