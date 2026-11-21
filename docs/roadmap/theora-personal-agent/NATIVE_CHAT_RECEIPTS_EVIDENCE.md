# Native whole-turn chat receipts

Date: 2026-10-02. These changes follow the immutable 2026.9.28 app build.
No app was assembled, staged, signed, launched or modified by this worker.
The results below establish source and isolated fixture behavior, not packaged
GUI acceptance, real model inference or external action success.

## Implemented contract

The native client negotiates `chat.capabilities` before sending any prompt.
It requires contract version 1, exact session identity and actual JSON booleans
for durable receipts and whole-turn terminal support. A legacy, silent or
malformed server receives no text command. Each submitted turn uses a fresh
canonical UUID as `msg_id` and opts in with `turn_contract_version: 1`.

Before WebSocket submission, the user row and request reference are saved to
the existing conversation endpoint. The save acknowledgement must match the
exact conversation ID and an integer message count. A failed or malformed
acknowledgement blocks dispatch while retaining the user draft and attachments.
This uses the existing store and save ordering; it introduces no alternate
conversation stack. The acknowledgement check is not a full byte-for-byte
conversation readback.

Only an accepted receipt followed by a terminal receipt for the exact current
session, connection, request and server-issued turn ends live processing.
Provider stream-final markers end an iteration, not the task. Intermediate
approval text, tool output, structured refusal/budget notices and error progress
retain their existing presentation without claiming whole-turn completion.
Trusted `payload.chat_turn` correlation must match before progress is displayed
during a tracked turn. Foreign or unscoped progress is ignored. An unresolved
thread also rejects unscoped legacy progress after reconnect.

Terminal records retain processing outcome, action outcome, approval IDs and
request/turn references. Failed terminals preserve observed errors and actual
partial text. `completed` means request processing finished. It never means an
external action succeeded. An uncertain action outcome remains explicitly
uncertain, including when processing has finished. Accepted receipts do not
mean delivered, read or seen by a person.

Stop sends one exact `chat.abort`. If acceptance has not arrived, Stop waits
for the server-issued turn ID. A cancellation acknowledgement does not stop
the processing indicator; only the matching terminal receipt does. Stop does
not close the socket or reconnect. Late acknowledgements cannot alter a
finished turn. Saved references grant no abort or execution authority on a
new connection.

On disconnect, runtime loss, shutdown, a lost send reply or the four-minute
terminal deadline, actual user/partial assistant text and the request reference
remain. The selected thread holds new prompts until a read-only status check
establishes its terminal state, or the user chooses a new thread. There is no
automatic prompt retry or replay. A new connection can negotiate and issue
`chat.status`; missing, active or malformed status remains unresolved.
Capability and status reads have bounded deadlines. Deadline checks bind the
exact connection and request/read ID. The task deadline is installed before
awaiting the send, so an early terminal cannot install a stale timer afterward.

Choosing a new independent conversation clears the prior selected thread's
recovery hold. Attachment removal after an awaited send checks both connection
and conversation revision/session, so an old send cannot remove another
thread's pending files after a terminal permits a thread switch. Existing
attachment content consent and rich chat/tool/reasoning metadata are retained.

## Checks actually run

From `ASOS/desktop-native`:

```sh
bash test_features.sh ChatTurn > /private/tmp/feral-native-chat-receipts-20261002.log 2>&1
```

Final exit status: **0**. The registered runner compiled the entire linked
native model and views against frozen sibling sources, then ran:

- ChatTurn: 3 state-machine fixture groups.
- NativeModel: 25 mocked HTTP/wire groups, including the original 20 groups.
- DesktopExperience: 39 assertions.
- ErrorPresentation: 5 assertions.

The new linked fixtures cover negotiation before any prompt, malformed version
types, exact presend save acknowledgement, reviewed attachment wire fields,
early Stop, one abort, cancellation acknowledgement versus terminal, progress
isolation, multiple provider iterations, uncertain cancellation, stale request
and connection deadlines, unresolved reconnect holds, passive terminal status,
saved references, missing status, a new independent thread, save failures, lost
submission replies, terminal arrival while send awaits, thread-switch attachment
mutation and retention of observed failure/action uncertainty.

Two initial runner failures were corrected before the final pass: a new private
fixture type required a private helper declaration; the new attachment fixture
used `id` instead of the existing `upload_id` wire field. Production attachment
semantics and existing assertions were preserved. Existing asynchronous NSLock
usage in the legacy harness produces Swift 6 migration warnings under the
repository's Swift 5 compile mode; it did not prevent the final pass.

After the linked pass, additional standalone tests checked exact status-deadline
expiry and completed-processing wording with unknown effects:

```sh
xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 -parse-as-library NativeChatTurnFeature.swift tests/NativeChatTurnFeatureTests.swift -o /private/tmp/feral-native-chat-receipt-tests
/private/tmp/feral-native-chat-receipt-tests
```

Both exited **0**, with all 3 standalone groups passing. Production sources and
linked model fixtures were unchanged after their successful linked run.
Targeted `git diff --check` exited 0.

## Source boundaries and remaining work

The native checks use disposable preferences, URLProtocol HTTP responses and
an injected frame transport. They do not exercise a real URLSession WebSocket,
SQLite server, provider or account. Backend/SDK registered-WebSocket tests and
trusted progress tagging are separate parent-owned evidence. This client
relies on those server contracts and existing local session/transport trust;
the UI request marker is not an authorization boundary.

Awaiting approval is a terminal processing outcome for that prompt's turn, not
proof the requested action was fulfilled. Subsequent approval execution remains
under the existing permission and tool policy contracts. No new receipt for
an entire multi-approval business task is invented by this slice. Existing
idle legacy progress presentation is retained only when there is no tracked
turn or unresolved recovery hold.

Cancellation cannot undo an already committed or noncooperative external
effect. Unknown/missing receipts cannot establish that no effect occurred.
New status reads have no execution or new-socket cancellation authority.

These source tests do not resolve or certify the separately reproduced native
layout-cycle crash. The next immutable candidate needs the shared text-layout
repair, linked regression reruns and actual UI acceptance for capabilities,
long conversation rendering, Stop, disconnect recovery and approval behavior.

## Frozen source SHA-256

| File | SHA-256 |
| --- | --- |
| `desktop-native/APIModel.swift` | `ab93df225ec528f7c67af1444e46fa2425ce0f5e7de1cf63c3fdc3cce1aa5b07` |
| `desktop-native/NativeViews.swift` | `96ba513c0731608b057493d3aa3beaee7bf358d1ddf29a0bf7ffb6b5e520cdbc` |
| `desktop-native/NativeModelTests.swift` | `5fe5e6a7382aa6f55b9f7de8c9c765eb4605a9abfda17498b3ad313342a280f7` |
| `desktop-native/NativeChatTurnFeature.swift` | `6c3fb36ba06d6bedd21514be791b9c9dde8e69e662abf8b234f6c93d7bdb81b0` |
| `desktop-native/tests/NativeChatTurnFeatureTests.swift` | `b5ffd7672871e3f594e0fcb599d22a760a41f663e1ab53c7ebb932bfd6e62948` |
