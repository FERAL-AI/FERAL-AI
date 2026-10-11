# Native scheduled-automation parity and routine review evidence

Date: 2026-10-02. Base publication: `66c7cd500`. Changes are source changes
after the independently staged 2026.9.28 artifact. They do not enter that
immutable bundle, and this report does not certify packaged UI acceptance.

## Implemented behavior

`NativeWorkflowFeature.swift` now exposes Scheduled automations in Workflows,
using the existing `/api/automations` create/list/delete contracts. This is
separate from the Automation destination's geofence and webhook controls.
The inventory read does not start the scheduler. No Run Now endpoint, private
execution callback, policy bypass or backend change is introduced.

Creation uses an explicit interval of 1 through 10,080 minutes and an action of
at most 8,000 UTF-8 bytes, with a selected conversation. It composes the exact
`every N minutes, <action>` instruction. The existing backend regex scans the
whole instruction and prioritizes some patterns over the prefix, so the action
cannot contain the scheduling words every, daily or weekly. The UI explains
this boundary. It does not promise arbitrary natural-language/event-trigger
parsing or silently default an unparsed instruction to hourly execution.

The review shows the interval, exact composed instruction, conversation and
background-action implications. Creation requires an exact acknowledgement,
safe per-routine detail and inventory readback proving id, CUSTOM type, enabled
state, owner session, cron, description and the complete three-field
natural-language payload. Extra payload authority such as `auto_confirm` does
not pass. Its receipt means an enabled schedule was saved, not that a task ran.

Reviews are issued by the existing native model, generation-bound, single-use
and expire after 120 seconds. The model retains at most 100 outstanding reviews
and prunes expired entries before issuing more. Selected conversation or
service changes clear them. This is a native review safeguard, not a replacement
for backend policy or authentication.

Automation deletion first reads its exact CUSTOM schedule and verifies the
selected conversation and inventory terms. The modal shows the captured stored
action. On confirmation, a fresh detail read must still match the captured
stable terms before DELETE. Success requires valid inventory absence and the
exact routine detail's explicit `Routine not found` response. It never treats
an acknowledgement alone as proof of deletion.

Existing routine pause/resume/delete now capture and display exact record
terms, use the same single-use/expiry guards, fetch safe detail before mutation,
and verify the resulting terms or exact absence. Dynamic last_run, next_run and
run_count are excluded from the stable comparison; action, scope, schedule,
enabled state and other returned fields are retained. Pause preserves any
existing disabled reason; resume clears it according to actual scheduler
semantics. The side-effecting GET `/api/routines` remains explicitly reviewed
and is never used for passive preflight or automatic refresh.

An attempted schedule mutation whose response or readback cannot establish the
result produces an unknown/unverified outcome with an instruction to inspect
stored state. There is no automatic mutation retry. The consumed review cannot
replay. This does not add durable request deduplication to the legacy backend.

## Validation

Final held-source standalone compilation exited 0, and execution passed **67
native Workflow fixture assertions**, exit 0. The initial sandbox compiler attempt was
blocked by Xcode's SwiftUI macro plugin sandbox. It was rerun with approved
`xcrun swiftc` access. An earlier fixture ternary caused a Swift type-inference
diagnostic and was simplified without weakening assertions. One intermediate
compile correctly rejected a source edit made during its run; that invalid run
is not counted as passing evidence.

Final direct compile command, from `desktop-native`:

```sh
xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 \
  -module-cache-path /private/tmp/theora-native-feature-tests-cache \
  -parse-as-library NativeRichText.swift NativePlainTextEditor.swift \
  NativeReviewSummary.swift NativeWorkflowFeature.swift \
  tests/NativeWorkflowFeatureTests.swift \
  -o /private/tmp/feral-native-workflow-parity-tests
/private/tmp/feral-native-workflow-parity-tests
```

The fixture matrix uses URLProtocol and in-memory disposable records. It covers
the existing workflows and new controls, ambiguous/oversized/unbounded input,
selected-session creation, exact canonical body, persisted field mismatch,
extra auto-confirm payload, used/expired/stale reviews, foreign owner/type,
changed action before mutation, false-empty inventory, duplicate ids, lying
delete acknowledgement, lost response after simulated commit and no replay,
routine action terms, and a connection change while fresh preflight is awaiting.
No jobs, providers, integration transmissions or actual app process is started.

A separate actual-source router/SQLite check passed three cases at 1, 60 and
10,080 minutes, with a Unicode local-note instruction. It used the pinned
`.venv/bin/python`, a new disposable `FERAL_HOME` and scheduler database, a
minimal FastAPI app including the real timeline/routines routers, and TestClient.
It did not run server lifespan, configure a callback or start task execution.
This validates real route/store shapes but is not full server middleware or
native-network acceptance. Confirmed results:

- Each POST saved the expected CUSTOM/enabled/session/cron/action payload.
- Real GET inventory and detail exposed the persisted record.
- DELETE removed it and detail returned HTTP200 `{"error":"Routine not found"}`.
- The legacy DELETE of a missing id still returns `success:true`, confirming
  the need for absence readback.

The router check exited 0 with the existing Starlette TestClient deprecation and
Pydantic schema-field warnings. No runtime or provider initialization was run.
Whitespace is checked against the two owned Swift files. Source remains held
unchanged during the final compile and fixture execution.

Frozen source SHA-256 values:

- Workflow source: `885d50a14d85fb247de9a073c5fcf060a6996c2e21217e521a43fe6e1c54783e`
- Workflow tests: `293a785f125e15fc00d7197072d70e068722c672c8833ea59da41496739539c0`

## Limits and remaining acceptance

Current backend writes have no atomic conditional terms or idempotency key.
Fresh native preflight reduces stale-action risk but cannot close a concurrent
client's write between GET and mutation. A manual newly reviewed duplicate
request remains possible; unknown responses require reconciliation. These are
explicit backend-contract dependencies, not exactly-once claims.

Routine REST readback omits recurring/timezone, so this card does not certify
those fields or redesign routine creation. Arbitrary scheduled-automation edits,
event trigger execution and Run Now remain separate cards. Existing cron policy
dispatch limitations identified in NATIVE_AUTOMATION_CONTRACT_PLAN.md are not
repaired by these UI controls. Already running work may continue after deletion
or pause.

Parent integration must run the linked native/model runner after all sibling
sources freeze. A later assembled candidate needs genuine disposable-profile UI
creation, review cancellation, relaunch/readback and deletion acceptance; no
real scheduled external action is needed for these storage receipts. Clean Mac
installation, signing/notarization/distribution and physical effects remain
separate release gates. The installed 9.28 app was neither launched, signalled,
replaced nor inspected by this worker.

Only `desktop-native/NativeWorkflowFeature.swift`, its existing Workflow test
file and this new evidence file belong to this implementation card. No
APIModel, backend, shared checkpoint, build script, staged resource or Git
mutation was performed.
