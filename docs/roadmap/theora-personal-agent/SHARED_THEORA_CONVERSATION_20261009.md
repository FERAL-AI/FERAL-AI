# Reviewed shared Theora conversation

October 9, 2026. Native source adds an explicit Browser action to select the
verified canonical conversation used by Theora. Immutable 9.47 does not contain
this change; new exact-source assembly and actual packaged interaction are pending.

The review captures the current thread, primary session, local runtime and
selection revisions. Confirmation refreshes primary identity, saves the current
thread and independently reads back its exact rich records before switching.
An absent shared record uses the existing atomic insert-if-missing operation and
verified readback, preserving any concurrent winning record. Separate history
is never inserted into shared model context.

Active voice, chat, uploads, unresolved recovery, changed primary/runtime and
expired or consumed reviews refuse switching. Chrome must first be disconnected
in its current owner. Pending connection/tab/view reviews also block switching.
The shared thread then requires a separately reviewed Chrome connection and tab
attachment; conversation selection grants no browser or action authority.

Executed frozen source evidence:

- 29 linked native model groups passed. New cases cover exact save/readback,
  cancellation/replay, primary/runtime drift, busy-state races, missing-record
  atomic creation, failed saves and incorrect readback.
- 118 browser fixture checks passed, including current/pending Chrome ownership
  and unknown-disconnect refusal.
- An actual NativeVoiceEngine starting-state fixture with injected audio proves
  the switch is refused during voice admission. It performs no microphone capture
  or provider operation.
- Production typecheck passed outside the sandbox. The initial sandbox run could
  not start Apple's SwiftUI macro plugin and produced cascading diagnostics.
  A preliminary voice-busy fixture lacked capability negotiation; actual voice
  admission correctly refused its setup. Both failed attempts remain separate.

Private final receipts: feral-oct9-shared-conversation-evidence.json,
feral-oct9-shared-model-tests-final-verified.log and
feral-oct9-native-948-typecheck-unsandboxed.log. These tests use mocked HTTP/wire
boundaries and do not establish an actual phone, Chrome account or inference task.

The detached-job execution session remains separate from its initiating chat.
This foreground selection does not implement a phone's background-job approval
bridge or a job-scoped Chrome delegation. Those require the explicit ownership
contracts recorded in [the iOS handoff](IOS_AGENT_HANDOFF.md).
