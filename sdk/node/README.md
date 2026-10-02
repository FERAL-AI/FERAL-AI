# Generic FERAL SDK for Node

Requires Node 22 or later for built-in Fetch, WebSocket, AbortController and
crypto. This `sdk/node` package is the generic Brain/plugin client; the hardware
SDK at `feral-nodes/ts-node-sdk` is a separate package with separate checks.

```ts
import { FeralClient } from '@feral/sdk';

const client = new FeralClient('http://localhost:9090', {
  bearerToken: process.env.FERAL_API_KEY,
  sessionId: 'my-project-thread',
  chatTimeoutMs: 60_000,
});
const receipt = await client.chatTurn('Inspect the project and summarize it');
console.log(receipt.processing_outcome, receipt.final_text);
if (receipt.processing_outcome === 'awaiting_approval') {
  console.log('Review required', receipt.approval_request_ids);
}
client.close();
```

The optional caller credential is an HTTP Authorization header or the first
WebSocket auth frame. It never goes into a chat URL or SDK log. Credentials do
not expand permissions; a device credential accepted for HTTP may not be a valid
operator/session chat credential. URL credentials, query strings and fragments
on the base URL are rejected. Proxy paths are preserved.

Each client has a stable isolated UUID thread unless a session is supplied.
`chatTurn(message, {sessionId, timeoutMs, signal})` may select a thread per call.
Same-client overlapping calls to the same thread refuse with `session_busy`;
different threads can run concurrently. Other surfaces sharing an explicit
thread remain subject to the server's attachment/delivery behavior.
Each thread retains one negotiated socket and its message listeners between
calls, so later messages use the live server history. Continuity lasts while
that connection stays alive. The last nonprimary disconnect still clears volatile
context; saved UI messages and durable receipts are separate from model history.
Persistence across client shutdown or server restart remains a backend follow-up.
Attaching an old explicit ID from a new client does not certify restored context.

`maxChatThreads` defaults to 8 (integer 1 through 64). New threads at the limit
refuse with `thread_quota`, preserving existing live history. Call
`closeThread(sessionId)` to release an owned thread, or omit the ID for the
default. Explicit close, idle disconnection, malformed protocol, timeout or
AbortSignal retires that ID: further prompts refuse with `context_lost` before
any new connection or command. Choose a **new** explicit ID for deliberate new
work after reconciling uncertain prior effects; it does not resume the old task.
Live and retired identity records are bounded to 1,024 per client, after which
`thread_identity_quota` refuses further IDs. Records are never silently evicted
or reset. `close()` permanently terminates the client; later chat refuses with
`client_closed`.

```ts
const client = new FeralClient('http://localhost:9090', {sessionId: 'thread-A', maxChatThreads: 4});
try {
  await client.chat('Remember violet maple 47 for this conversation');
  const answer = await client.chat('What did I ask you to remember?');
  client.closeThread();
  const fresh = await client.chat('Start a separate conversation', {sessionId: 'thread-B'});
} finally { client.close(); }
```

Chat negotiates `chat.capabilities` before sending any user command. An older
server, wrong session or missing version/durable/whole-terminal support refuses
without a task prompt. No automatic legacy downgrade, reconnect or replay occurs.
Only matching accepted and terminal request/session/turn IDs produce a receipt;
ordinary text, approvals and model-round stream terminals cannot finish a turn.

`ChatTurnReceipt.processing_outcome` is completed, awaiting_approval, failed,
cancelled, outcome_unknown, unavailable, refused or budget_exceeded. Completed
means response processing ended. `action_outcome` remains `not_asserted` or
`unknown`; this is not proof of purchase, delivery or other external effects.

`chat()` returns final prose after completed processing. Otherwise it raises
`ChatTurnError` with its `code` and server `receipt` where available. The total
deadline includes connection/auth/negotiation; `ChatTurnTimeout` rejects partial
responses. AbortSignal or `close()` closes only this client's transport and does
not assert cancellation or reversal of external effects. Reconcile uncertain
results before another effectful invocation. The SDK never automatically approves.

HTTP uses `/health` and `/skills` with JSON Accept headers. `invokeSkill(skillId,
endpoint, args, {sessionId, confirm:false})` and `createNote(content, tags,
options)` use `/api/tools/execute`. Explicit `confirm:true` asserts that the
caller reviewed and authorized the exact action; the server may still refuse.
HTTP 200 application denials remain returned envelopes. Inspect `success`,
`status_code`, `data` and `error`; do not mark the action executed merely because
HTTP succeeded. Non-success HTTP status raises `FeralHTTPError` before parsing;
redirects are not followed. Malformed JSON/schema or a transport/deadline failure
raises a bounded diagnostic. No automatic HTTP retries occur.

```sh
npm ci
npm test
```

The test runner builds the generic package and runs Node's built-in tests. Tests
use controlled Fetch/WebSocket transports, not accounts or live model inference.
Python SDK integration separately checks the shared protocol against the actual
registered server, SQLite receipts and real controlled orchestration. See
[tracked-turn evidence](../../docs/roadmap/theora-personal-agent/SDK_WEBSOCKET_EVIDENCE.md)
and [live thread evidence](../../docs/roadmap/theora-personal-agent/SDK_THREAD_CONTINUITY_EVIDENCE.md).
