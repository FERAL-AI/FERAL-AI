# Generic SDK tracked-turn evidence

DEV-01B, October 2, 2026. This slice updates the generic Python SDK client,
public exports, README and new WebSocket tests; the generic Node SDK client,
types, public exports, README, test script/runtime declaration and new tests.
The separate hardware SDKs and plugin/device implementations are preserved.
Parent integration owns dependency locks, CI wiring, Git publication and the
next immutable app. Checks ran on the next working wave after checkpoint
`739a20c0b56d2a2aa9956cde395a79b6dd5e0935`, using the frozen CORE-04 contract.

## Confirmed defects before this slice

The registered session endpoint only greets the default primary connection;
explicit session connections receive no greeting. Its greeting is ordinary
`text_response`, not a `greeting` frame. The Python SDK awaited the greeting
before sending; the Node client sent only after a nonexistent greeting or a
text containing `connected`. The compiled old Node client was executed against
the actual greeting shape and returned `How can I help?` with **zero user
commands sent**. Both clients could report partial/timeout prose as success.

The server selects identity from the session query, overriding incoming payload
identity. Its client WebSocket supports an initial auth frame, rather than the
HTTP Authorization header contract. Default primary-thread reuse was unsuitable
for independent SDK sessions. Per-provider `stream_delta.is_final` can precede
tools, retry rounds and approval notifications; `text_response` also does not
prove the whole tracked turn or external action succeeded.

## Implemented contract

1. A client retains an isolated default UUID thread. Callers may supply an
   explicit session identity on the constructor or method. Identity is encoded
   in the query, preserves proxy paths, and is bounded to the existing 1,024
   character contract. Empty, padded and control-character identities refuse
   before connection. Same-client overlapping requests for one thread refuse;
   distinct explicit threads can run independently.
2. Optional caller credentials go in the first auth frame and HTTP headers,
   never chat URLs. A private disabled Python WebSocket logger prevents the
   transport's DEBUG sent-frame logging from recording the auth body. SDK
   diagnostics contain bounded codes, not private server bodies or URLs.
3. Before sending any prompt, chat issues a correlated read-only gateway
   `chat.capabilities` request. It requires version 1, durable receipts,
   whole-turn terminals and the exact query-selected session. Unknown methods,
   old greetings, malformed responses or unavailable support produce no user
   command. There is no automatic legacy downgrade.
4. A supported command uses a canonical UUID `msg_id` and
   `payload.turn_contract_version=1`. The client validates the accepted
   request/session/server-turn IDs and waits for that exact terminal receipt.
   Text, stream finals, approvals and unrelated request frames cannot resolve
   it. Unsupported or malformed receipts are explicit failures.
5. `chat_turn()` / `chatTurn()` returns a typed processing receipt. Outcomes
   include completed, awaiting_approval, failed, cancelled, outcome_unknown,
   unavailable, refused and budget_exceeded. `completed` describes processing;
   `action_outcome` is `not_asserted` or `unknown`. It is never a blanket purchase,
   message delivery or other task-effect success claim.
6. `chat()` returns nonempty final prose only after completed processing.
   Other outcomes raise `ChatTurnError` with a receipt where available. A
   total deadline covers connection, auth, negotiation and execution;
   `ChatTurnTimeout` never returns partial text. Premature closes and auth
   denial are bounded failures. Known correlated request-error codes are
   preserved, while arbitrary private diagnostics are not echoed.
7. Cancellation closes owned transports only. No SDK abort, auto-confirmation,
   retry, reconnect or replay is sent. This does not claim server cancellation
   or reversal of prior effects. Reconcile through server/effect receipts before
   issuing another effectful command.

The Node HTTP client now matches the already repaired Python HTTP contract:
JSON `/health` and `/skills`, canonical `/api/tools/execute` invocation with
explicit confirmation defaulting false, optional session identity, note-saving
through its existing skill, and status-before-JSON error handling. HTTP 200
application denials remain returned envelopes. Redirects are not followed.
Dashboard/system-info signatures remain compatible; these helpers validate an
object boundary, rather than claiming exhaustive nested schema validation.

## Actual final checks

Parent integration additionally ran the locked TypeScript5.9.3 compiler and exact
`npm test`:53 passed. CI adds the generic Node22 SDK separately from the device
SDK. A focused mypy check found one enum-input type diagnostic; explicit string
validation corrected it, final Python72 tests passed in3.05 seconds and focused
client/chat_turns mypy passed (untyped bodies remain unchecked). Full remote CI
for this source wave remains separate.

From ASOS, with the existing Python environment:

```sh
FERAL_HOME=/private/tmp/feral-sdk-contract-final-home FERAL_DATA_HOME=/private/tmp/feral-sdk-contract-final-data .venv/bin/python -m pytest sdk/python/tests -q --no-cov -p no:randomly --tb=short
```

**72 passed, 7 warnings in 5.11 seconds**: 25 existing HTTP cases plus 47
WebSocket cases. Fake-wire cases check exact auth/capability/command ordering,
identity, request/turn correlation, approval/failure outcomes, total deadlines,
malformed/closed connections, cancellation, concurrency and zero-task legacy
refusal. Public root imports are exercised.

Four new integration cases reuse the backend worker's frozen `tracked_client`
fixture. They route the actual registered `server.client_session` through
FastAPI/TestClient, with actual MemoryStore SQLite receipts and a real controlled
orchestrator. The SDK's async wire is adapted to TestClient I/O; it is not an
alternate handler or protocol implementation. Tests confirm:

- A multi-round stream with model prose, an intermediate model terminal and
  a tool result continues until the exact whole-turn terminal; committed
  receipt readback matches the SDK's request, turn and final text.
- Actual off-loopback auth policy accepts the supplied fixture operator key
  in the first frame and rejects the wrong key without invoking preparation.
- Actual missing durable-contract support sends no user command and invokes
  no task preparation.

The fixture patches prompt refinement/provider/tool effects and uses disposable
storage and in-memory OS-vault wrappers. No actual model, account, payment,
message or production deployment was used. Warning output consists of existing
Starlette/model/lifespan deprecations and the imported core marker not registered
under the SDK-only pytest root.

The generic Node client was compiled with existing locally installed TypeScript,
without installing dependencies or changing another SDK:

```sh
node feral-nodes/ts-node-sdk/node_modules/typescript/bin/tsc --project sdk/node/tsconfig.json --outDir /private/tmp/feral-generic-sdk-contract-dist-20261002
FERAL_SDK_DIST=/private/tmp/feral-generic-sdk-contract-dist-20261002 node --test sdk/node/tests/client.test.cjs
```

TypeScript compilation passed. On installed **Node 25.4.0**, **53 tests passed**,
zero failures/skips, in 2,776.68 milliseconds on the final run. Node tests use
controlled WebSocket/Fetch transports and actual compiled public-root imports;
they independently cover shared-wire semantics and registered HTTP request
shapes. They do not certify real Node network transport, TLS or Node 22 runtime
behavior. Parent CI owns the generic package's `npm install` / `npm test` path;
the existing hardware SDK's green CI is not evidence for this generic package.

Ruff passed all owned Python files using the repository's E/F/W selection and
existing exclusions. The owned-file whitespace check passed. Python's existing
declared minimum is 3.11; StrEnum and asyncio.timeout do not raise that minimum.
No blanket type baseline/ignore was introduced by this SDK work.

## Corrected fixture issue and remaining limits

The initial SDK-only registered-WebSocket run failed at server import because
the imported core fixture did not bring core conftest's home isolation with it.
The default profile was attempted; the sandbox rejected its database schema
write as read-only. That failure is not acceptance evidence. It also produced
vault-unavailable metadata warnings. The new SDK module now fences disposable
homes before any server import and replaces all OS-vault wrappers with an
in-memory store. The corrected file-only run and final full-SDK run passed.
No successful personal-store write or real account effect occurred.

These are local source/fixture contracts, not a signed app or clean-machine
installation. Actual remote network/TLS/proxy transport, the declared Node
minimum, broad combined regression, restart/receipt recovery through consumers,
and next rebuilt native app acceptance remain integration/release gates. There
is intentionally no automatic task replay after uncertainty or convenience SDK
API claiming effect cancellation. Root release records must keep the previously
built candidate separate from this next source wave.

Source review also confirms a continuity limitation: each SDK invocation closes
its own socket. On the last nonprimary attachment's disconnect, the registered
server calls `Orchestrator.on_session_disconnect`, which removes in-memory
conversation history, and clears working memory. Stable SDK IDs and SQLite turn
receipts preserve correlation, not necessarily continuous conversational context.
There is no acceptance claim for multi-turn conversational memory here. A
persistent negotiated SDK connection or durable thread restoration needs its
own session/concurrency/cancellation tests before claiming chat continuity.
