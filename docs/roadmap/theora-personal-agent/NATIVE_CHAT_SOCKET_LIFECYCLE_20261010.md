# Native chat connection and tool-card repair, October 10

## Observed failure

Actual 9.59 native-composer testing reached the bundled backend and GPT-5.4
Responses tool execution. A second submission failed before confirmed admission,
and its status action also failed although the backend remained healthy. The
native client retained receipt readiness for a non-running WebSocket. Reopening
the exact saved conversation restored passive status negotiation without replay.
The durable store retained only the first request; missing receipt alone does
not prove no external effect occurred.

The socket shared an HTTP URLSession configured with a 300-second resource
lifetime. A real inert loopback WebSocket and Foundation client reproduce the
failure class with a shortened 3-second lifetime: shared-session task becomes
completed; a dedicated default-lifetime session remains running. The exact
termination trigger in the observed 9.59 run is not established from this fixture.

## Repair

Chat uses a separate long-lived URLSession; ordinary HTTP requests keep their
bounded resource deadline. Exact-current socket closure, completion, receive or
send failure invalidates readiness before persistence awaits. Late callbacks
from older sockets and ordinary HTTP completion cannot retire a replacement.
Pending request identity and uncertainty remain intact. Explicit Check status
renegotiates the connection and reads the same durable request; no prompt replay.
Session, selection, runtime, admission and attachment identities are checked
across new preflight awaits. A new Send may retain its draft while fresh
capability negotiation is pending; it does not queue or automatically resubmit.

Separately, native tool-result cards now display bounded text from known nested
Card/Text trees. Unsupported widgets and actions remain inspection-only; exact
raw metadata is retained. Ordinary saved messages were intact in the live case;
no transcript-restoration repair is asserted.

## Verification

The original native socket fixture fails its exact closed-socket readiness
assertion. The repaired frozen 54-input fixture passes 30 native model groups,
including actual delegate handling, independent second request, passive status
recovery, stale callbacks, thread-switch and attachment-review races. These use
mocked HTTP and inert socket tasks; the separate lifetime probe is real loopback
networking. The integrated original-source gate is recorded in WORK_STATE.

The integrated rich-chat fixture passes its 15 existing groups and ten new
assertions for official nested cards, metadata retention, unknown widgets,
connection/session scope and text/node/depth bounds. Its regression fails before
the repair. This is native-source execution, not a full UI outcome.

Run the socket gate with:

```sh
python3 desktop-native/acceptance/native_chat_socket_lifecycle_probe.py
```

The helper adds a private-access extension only to a temporary compile unit;
no production fixture authority seam is shipped. A fresh optimized app build,
strict signature and packaged-source verification, two actual native-composer
requests, and FERAL-controlled Chrome outcome readback remain the acceptance
boundary. Physical phone/glasses acceptance is independent.
