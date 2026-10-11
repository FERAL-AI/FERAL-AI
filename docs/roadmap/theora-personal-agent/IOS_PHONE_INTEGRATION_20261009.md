# October 9 iOS conversation and phone intake source integration

## Subsequent phone dispatch-ownership correction

The initial responsive-intake change fenced stale replies but did not revoke
new tool dispatch inside a collaborator that suppressed cancellation. Task-local
ownership now follows inherited child contexts and is checked by the existing
central dispatch guard. It captures the exact phone socket, runtime, orchestrator
and memory instance; replacement or disconnect cannot reuse that authority.
This does not change native bootstrap generations or unrelated sessions.

The existing executor also rechecks ownership immediately before queued backing
implementation and subprocess-thread work starts. Already-started external work
remains uncertain; cancellation is never reported as rollback or permission to
repeat it.

Frozen targeted verification passes 47 tests across phone intake, native turn
leases and retained-task references, 63 warnings, 5.03 seconds. Independent
actual ToolRunner/executor probes pass nine cases, one warning, 0.55 seconds,
including cancellation suppression, replacement, inherited children, unrelated
session compatibility and both queued pre-effect races. CI-rule Ruff and scoped
whitespace checks pass. Evidence: feral-oct9-phone-owner-fix-evidence.json,
feral-oct9-phone-owner-fix-final-tests.log and
feral-oct9-phone-final-independent-probes.log. Combined source acceptance and a
new exact-source application remain pending; immutable 9.47 is unchanged.
These tests use inert effects and do not establish physical phone connectivity.

## Initial source integration

### Actual approval node-sender correction

An actual BrainState probe found push_to_session_nodes passing raw dictionaries
to send_to_daemon, whose object serializer requires FeralMessage. The exception
was caught before any matching node received a frame. Approval fixtures that
replaced the state sender did not expose this failure.

Raw approval dictionaries now use the existing HUP queue path. A shared internal
helper reports accepted socket sends; the existing voice callback still returns
None. Only matching current nodes are selected. Missing and failed sockets are
not counted. This is socket acceptance, not delivery or read/seen confirmation.

Frozen source verification passes 53 tests, one skipped, 18 warnings, 2.24
seconds. Actual ToolRunner and Orchestrator publishers reach the real state
sender with inert sockets; tests cover HUP envelope/schema parsing, foreign
session exclusion, missing/replaced/failing sockets and voice callback
compatibility. Ruff and whitespace checks pass; affected-file typing retains
only the existing optional-browser diagnostic. Private receipt:
feral-node-push-release-receipt-20261009.json. Combined source and application
acceptance remain separate. Independent transport review passes eight checks.
Final frozen combined source verification passes 4,734 tests, 22 skips across
232 suites, zero source drift; full Mac typing adds zero diagnostics versus the
pre-wave runtime and removes three. This fixes ordinary outbound transport; it does not
implement originating-phone background review translation or job browser grants.

This is local implementation and bounded verification, separate from installed
device acceptance. Theora iOS baseline is
`dfe2b08541e597c738bacd5fdc61275fc4f476a3`. Backend integration was captured from
ASOS head `3da563c4d9cbb482a7ce72804fc7da6ec98192b1` plus the current source
wave. Pre-existing iOS project/vendor changes and unrelated ASOS worker changes
were preserved. The immutable [Mac 9.47 artifact](NATIVE_9_47_ACCEPTANCE.md)
does not contain this new phone scheduling fix. No iOS push, installation,
account operation or personal profile migration occurred.

## Implemented behavior

Theora displays both existing providers' saved messages in one timeline, retaining
their internal turn ordering, repeated messages and attribution sources. The
existing voice frontend exposes independently available health and camera tools
together with hardware-free tools and ask_feral_brain. The Computer route remains
an explicit choice of the direct FERAL provider. Configured providers and the
original stores are preserved. New Chat clears the screen until app restart;
it does not erase those stores or the Mac's canonical conversation context.

Socket Connected means node_ack, with separate shared-conversation readiness.
Primary lookup errors are visible; there is no silent phone-node fallback.
Phone-local/unspecified addresses are refused. The phone shows the resolved shared
ID so Chrome can be attached to the same conversation on the Mac. Existing
native pairing tokens remain subject to the current allowlist, PIN, expiry and
revocation rules. REST requests use the captured socket credential/endpoint.

Text and delegated requests retain exact request/session/socket-generation
identity and bounded deadlines. A timeout or disconnect reports unknown outcome,
retains correlation for a possible late result, and never replays the action.
Response errors remain visible alongside partial prose. Empty capability grants
remain empty. Approval cards remain pending through submission; only a valid
matching decision removes them. Nested execution failures remain failures, and
approval acceptance never asserts a verified external outcome.

The legacy /v1/node chat_request handler now uses retained, bounded owned tasks
instead of awaiting a whole Mac task in its receive loop. Heartbeats, node action
responses and voice control can be received while a request waits. Up to 16
active/queued requests per socket are retained; same-session phone work serializes.
The existing agent lease, prepared scope, ToolRunner policy and approvals remain
the admission path. Replacement/runtime checks fence response and lifecycle
mutations, and disconnect retains bounded draining cleanup. This does not add a
second executor or undo already-started effects.

## Executed checks

- Actual production FeralHUPModels.swift and ToolDefinitions.swift compiled with
  the standalone Swift harness: **184 assertions passed**. Covers wire models,
  errors plus nonempty text/sources, exact request/session/generation and late/
  duplicate replies, bounded retention, malformed primary identity, forged/
  partial approval receipts and nested execution failure, loopback refusal,
  all four hardware availability states and timeline ordering/repeated turns.
- Full existing w.xcodeproj/Theora **unsigned device-target build succeeded**,
  exit 0, with Xcode 27 and current cached dependencies. All changed application
  sources compiled. No XcodeGen regeneration. This is not a signed install or
  UI/audio/physical-device test; pre-existing concurrency/vendor warnings remain.
- Frozen copy of the current backend inputs: **274 passed, 2 skipped, 299
  warnings in 16.51 seconds**, exit 0; all recorded Python/config hashes remained
  unchanged during verification. Current CI-rule Ruff passed for the three
  changed Python files. This includes 15 new registered-ASGI scheduling/lifecycle
  cases with real disposable pairing/checkpoint storage and inert providers.
- Independent review added ownership checks after scope exit and awaited error
  delivery, and during registration/runtime replacement. The earlier isolated
  base-head patch also passed 274 cases. A current working-tree run passed 274
  but three shared runtime files changed during that run, so the frozen copy
  above supplies the accepted integration result instead.

Initial failed/sandbox-limited attempts are retained in private evidence: the
worker's first collection omitted FERAL_HOME and default SQLite writes were
refused; subsequent runs use disposable homes. Early cleanup and fixture faults
were repaired before the final suites. The first Xcode attempt failed under
sandboxed dependency/build-service access; approved full builds subsequently
succeeded. These attempts are not counted as passes.

Reproduce the iOS Foundation suite from its checkout root:

```bash
python3 -B scripts/test_ios_feral_contracts.py
xcodebuild build -project ios/w.xcodeproj -scheme Theora \
  -destination 'generic/platform=iOS' -configuration Debug \
  -derivedDataPath "$EVIDENCE_ROOT/ios-build" \
  -disableAutomaticPackageResolution CODE_SIGNING_ALLOWED=NO
```

Use an authorized vendor SDK checkout, existing project and configured Xcode.
The simulator is unsupported by the vendor framework slices. From feral-core,
with its pinned interpreter and a disposable FERAL_HOME set before collection:

```bash
python -B -m pytest -q --no-cov -p no:cacheprovider -p no:randomly \
  tests/test_phone_chat_responsive_intake.py \
  tests/test_daemon_session_phone_branches.py \
  tests/test_phone_voice_and_surface_defaults.py \
  tests/test_runtime_context_ingress.py tests/test_pair_token_rest_auth.py \
  tests/test_existing_chrome_approval_integration.py \
  tests/test_existing_chrome_connection.py tests/test_existing_chrome_routes.py \
  tests/test_phone_chat_parity.py tests/test_chat_request_single_frame.py \
  tests/test_hup_protocol.py tests/test_hup_envelope_and_grants.py \
  tests/test_server_websocket.py
python -B -m ruff check --no-cache --select=E,F,W \
  --ignore=E501,E402,F401,W291,W293 api/server.py api/phone_chat_intake.py \
  tests/test_phone_chat_responsive_intake.py
```

The handoff publication note defines EVIDENCE_ROOT. Private local receipts are
ios-phone-docs-20261009/ios-source-receipt.json,
feral-phone-integrated-20261009-hcjlk8w7/{receipt.json,pytest.log,ruff.log},
and feral-phone-responsive-20261009/{manifest.json,REVIEW.txt,pytest-final-v2.log}.
They record source hashes and scopes, not shipped archives. Reviewed patch SHA256:
`8a20c2807e29ce5267f4d253a18f63f1a92a809e2a78c9104b6495a7ca1174c3`.

| Integrated input | SHA256 |
|---|---|
| api/server.py | 1a06423c51f76aebba70abec4d3fa4fe7a31bddd3862f582eedcdf09310ad8f4 |
| api/phone_chat_intake.py | d67eaaa0eb04f756bcd481d3bc131693c2ad10ffc0f7067dd6e92e4ead3fd65f |
| tests/test_phone_chat_responsive_intake.py | 8e45c294506c03c80859294acc81f2d33a6a01bfe94090bd07db5f1cc45dd0a4 |
| iOS FeralHUPModels.swift | 7ea0c372edd17da1d51c55f649829c5575121605ef7b0bb4282e14074779bace |
| iOS ToolDefinitions.swift | 7c49ea14e03620434d18fa32bc5f286d4bfaa0a5d4646a3de553e05d87f89fe8 |

## Remaining acceptance

1. Identify the active phone's endpoint, displayed error, Local Network
   permission and installed Mac backend identity. The original physical failure
   has not been reproduced or attributed from these fixtures.
2. Install a reviewed iOS build and exact-source Mac candidate in an owned test
   environment. On actual Wi-Fi/ATS/TLS paths, pair and resolve the shared session;
   select that canonical conversation on the Mac and explicitly attach Chrome.
   Run a read-only browser request, then timeout/disconnect/approval refusal
   without effect replay. Phone pairing cannot bypass local-only browser routes.
3. Finish authoritative cross-device memory/events, negotiated durable phone task
   status and exact cancellation. Desktop /v1/session turn receipts are separate
   and are not available to native pair-token clients by assumption.
4. Verify route handoff, old audio rejection, Bluetooth/calls, locked/background
   phones and hardware permissions on actual devices. The current node audio
   contract lacks complete same-socket stream identity; owner flags alone do not
   establish that fence. Approval system speech and full-duplex conversation
   during delegated work need physical audio acceptance.
5. Keep new signed builds, clean-machine installation, credential recovery,
   real model/provider/account outcomes and the existing product gates open.
   Source scheduling responsiveness does not establish durable multitasking or
   continuous simultaneous speech across jobs.
