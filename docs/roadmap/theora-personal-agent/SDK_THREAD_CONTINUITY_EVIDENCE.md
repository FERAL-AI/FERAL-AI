# Generic SDK live thread continuity

DEV-01C, October 2, 2026. Implemented against checkpoint
`4e19f07f8603196fd73ecdafd0e127dbad865e69` and its existing CORE-04 registered
session contract. The worker changed generic Python/Node clients, their README
files, new thread tests, and narrowly adjusted older socket-lifetime test
expectations. Public exports, plugin/device implementations and the backend
remain unchanged. Parent integration owns CI, locks, Git publication and app
acceptance. This evidence does not certify a new native candidate.

## Confirmed cause

The previous generic clients retained an isolated session UUID but opened and
closed a socket for each turn. `api/server.py` calls
`Orchestrator.on_session_disconnect()` and clears working memory after the last
nonprimary attachment leaves. The orchestrator removes that session's model
history. The next command initializes an empty history under the same ID.

Existing mechanisms have different purposes:

- `_prepare_chat_turn_context()` records/refines the new user prompt; it does
  not restore durable model history.
- `memory.store` conversation records hold saved UI messages. Native
  `APIModel.openConversation()` restores their presentation, explicitly without
  injecting them into model context.
- `BrainState` restores the shared primary thread's JSON snapshot at startup.
  This is not per-thread SDK persistence.
- SQL session snapshots and `/api/session/restore` explicitly replace runtime
  history/working memory. Their restore path lacks the orchestrator's active-turn
  session lock/revision fence. Automatically calling it during reconnect could
  overwrite concurrent work. It is not used by this SDK repair.
- Durable CORE-04 receipts record acceptance and processing outcomes. They do
  not contain a restored provider transcript or permission to replay effects.

These are source-inspected distinctions. The two new production-route tests
also execute the actual disconnect and model-context paths with disposable state.

## Implemented contract

Each exact session ID has one retained, authenticated and negotiated socket.
The server binds every command to its socket's query-selected ID, so independent
threads require independent sockets. One reader/listener stays active between
turns, detecting idle closures. Authentication and read-only `chat.capabilities`
negotiation occur once on each connection before the first prompt. Later turns
have fresh request UUIDs and exact server turn correlation; old/foreign receipts,
provider-round stream finals and approval prose cannot complete a new turn.

Same-client overlapping calls to one thread still refuse `session_busy`.
Different explicit threads run concurrently and retain distinct model histories.
The live-channel limit defaults to eight and is configurable from 1 through 64
with Python `max_chat_threads` or Node `maxChatThreads`. `thread_quota` refuses a
new channel without closing an existing thread or discarding its history.

`close_thread()` / `closeThread()` releases an owned thread and retires its old
ID. Omitting the argument selects that client's default thread. An idle closure,
protocol failure, deadline or caller cancellation also retires the affected ID.
Further prompts to a retired ID fail `context_lost` before any reconnect or
command. A deliberate **new** ID remains usable under the quota; it starts new
work and does not resume or reconcile the interrupted task. Live plus retired
identity records are bounded to 1,024 per client; `thread_identity_quota` refuses
new IDs beyond that bound rather than evicting records. Client `close()` is
permanent; subsequent chat refuses `client_closed`.

The deadline covers connection, authentication, negotiation and processing.
Connection establishment itself is held in the owned Python reader task, so
close/cancellation during a stalled connection releases that task without a
prompt. Python holds readers through transport cleanup even after idle loss.
Node clears message/open/error/close listeners and owned channel references on
retirement. Client close affects only its owned channels. No abort, retry,
reconnect, request replay, confirmation, snapshot restoration or new permission
is sent. Closing transport is not proof that server work or earlier effects were
cancelled or reversed. `completed` receipts retain their processing-only meaning;
other verified terminal receipts, including awaiting approval, preserve the live
channel and remain explicit outcomes.

Python transport frames retain the private disabled debug logger so auth bodies
are not logged. Errors contain bounded codes, not server payloads/URLs. New
socket interfaces use a narrow structural protocol rather than introducing an
untyped new transport surface.

## Actual checks

Pinned macOS Python 3.11.15:

```sh
FERAL_HOME=/private/tmp/feral-sdk-thread-final-home FERAL_DATA_HOME=/private/tmp/feral-sdk-thread-final-data .venv/bin/python -m pytest sdk/python/tests -q --no-cov -p no:randomly --timeout=15 --timeout-method=thread --tb=short
.venv/bin/python -m ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 sdk/python/feral_sdk/client.py sdk/python/tests/test_client_websocket.py sdk/python/tests/test_client_threads.py
.venv/bin/python -m mypy sdk/python/feral_sdk/client.py --follow-imports=silent --ignore-missing-imports
```

Python: **92 passed**, seven existing imported-fixture/dependency warnings.
Ruff passes; focused client mypy reports no issues. The twenty new thread
cases include two actual registered ASGI tests using real SQLite receipts,
production orchestration and a recording provider. Three sequential turns use
one auth/capability exchange and distinct request/turn IDs. Recorded second/third
provider requests contain preceding user and assistant messages. Two simultaneous
threads keep separate context, query identities, receipts and attachment counts.
Closing their final attachments exercises actual volatile-history cleanup.

Controlled transport cases verify idle closure, active and connection-phase
close/cancel/deadline behavior, zero later reconnects/prompts on lost threads,
new deliberate IDs, direct idle-reader shutdown, stale terminal rejection, bounded identities, no quota
eviction, reader/socket cleanup and absence of SDK server-abort commands.
The previous HTTP/auth/unsupported-server/correlation suites remain included.

Node 25.4.0 with generic SDK's local TypeScript 5.9.3:

```sh
node sdk/node/node_modules/typescript/bin/tsc --project sdk/node/tsconfig.json --outDir /private/tmp/feral-generic-sdk-thread-dist-20261002
FERAL_SDK_DIST=/private/tmp/feral-generic-sdk-thread-dist-20261002 node --test sdk/node/tests/client.test.cjs sdk/node/tests/threads.test.cjs
```

Compilation passes; **72 passed**, zero failed/skipped. The nineteen new cases
cover retained negotiation, exact fresh request/turn IDs, simultaneous threads,
same-thread overlap, stale events, idle/active/cold-connection interruption,
zero resubmission of lost threads, deliberate new IDs, bounded channels/identity
records and listener cleanup. Node transport tests use controlled WebSocket
objects, not a live network. Python's production-route integrations validate the
shared server protocol separately. Node 22 and Ubuntu remain CI acceptance,
not a claim from this macOS run.

## Failed development checks and correction

The first Python run hung in an imported ASGI fixture's teardown after auth
refusal. A reader already detected loss and was releasing its connection, but
the awaiting caller cancelled that same cleanup a second time. Also, one socket
close exception could skip context-manager exit. Retirement now leaves a reader
already performing cleanup uncancelled, awaits it, and attempts context-manager
exit independently of socket close. Subsequent full SDK and new production-route
checks pass; the hung run is not counted as acceptance.

The first Node run had seven old tests expecting two sockets for two explicit
invocations. They now assert one retained socket, two caller commands and explicit
client close, preserving all refusal/correlation assertions.

All fixtures use disposable homes/data and in-memory OS-vault wrappers. No
personal profile, account, message, purchase, installed model or daemon was used.

## Remaining limits

This provides live connection continuity, not durable independent threads after
process shutdown, server restart or network loss. Creating a new client with an
old explicit session ID does not prove that history survived. Retired IDs are
remembered only inside their owning client instance. The SDK never restores a
saved UI record automatically. Durable continuation needs a backend checkpoint
contract under the exact session lock/revision, bounded tool-aware history and
working-memory validation, deliberate resume semantics, and read-only
reconciliation of uncertain turn/effect outcomes before further action.

Supported server deployment remains a single-user local installation. Selecting
an isolated session ID is not a multi-user owner ACL or a new authorization grant.
Other surfaces deliberately sharing the same session remain subject to the
server's attachment/delivery and context limits. No native, iOS, Linux,
clean-machine installation or real-provider acceptance is asserted by this card.
