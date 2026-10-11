# CORE-04: durable correlated turns and exact cancellation

2026-10-02. Implemented on base `739a20c0b56d2a2aa9956cde395a79b6dd5e0935` plus this card's working tree. The previously staged native candidate remains an independent immutable artifact. This card does not certify that candidate or the complete product.

## Confirmed defects

Read-only reproductions used actual production methods with controlled fixtures:

- `GatewaySession` returned `chat.abort` status `aborted` while the original `chat.send` was still running. `chat.send` also awaited the full turn within the socket receive loop, preventing that socket from processing an abort until completion.
- Python SDK `chat()` returned provider prose at the first per-round `stream_delta.is_final`, leaving a subsequent tool result and actual final response unread. Its supplied session never reached the URL, and its bearer token was used only for HTTP.
- Cancelling queued turn B called session-wide subagent teardown while active turn A held that session's lock. Exact cancellation of a Python task alone would not have isolated B from A's child agents.

## Additive wire contract

New clients first send the existing typed gateway request `chat.capabilities`. It performs no task submission or memory write. Its response advertises `turn_contract_versions: [1]`, `durable_receipts: true`, `whole_turn_terminal: true`, and the server-bound `session_id` only when the actual tracked transport and receipt store are available. An old server's unknown-method response must stop the SDK before it submits any user prompt.

An opt-in request uses `text_command`, existing canonical UUID `msg_id`, and payload `turn_contract_version: 1` alongside text, context and attachments. Existing clients without that opt-in retain the old response path. Neither a prose frame nor a provider's `is_final` certifies whole-turn completion.

`chat_turn_accepted` carries:

```json
{"contract_version":1,"request_id":"client UUID","turn_id":"server UUID","session_id":"bound thread","status":"accepted","durable":true,"replayed":false}
```

`chat_turn_terminal` carries those identities plus `processing_outcome`, `final_text`, `action_outcome`, `approval_request_ids`, `durable` and `replayed`. Processing outcomes are `completed`, `awaiting_approval`, `failed`, `cancelled`, `outcome_unknown`, `unavailable`, `refused` and `budget_exceeded`. `completed` means orchestration ended with a response. It does not mean every requested action succeeded. `action_outcome` is `not_asserted` or `unknown`; this card never invents an external success or a rollback. Terminal delivery occurs only after its SQLite commit.

Gateway `chat.send` opts in with `params.turn_contract_version: 1` and UUID `req.id`. Its `res` returns acceptance promptly; event `chat.turn_terminal` wraps the identical terminal payload. `chat.abort` requires exact `turn_id` and `request_id`, checks the authenticated connection's bound session and the original socket owner, and reports `cancel_requested`, never a fabricated completed cancellation. Payload session fields cannot redirect these controls. `chat.status` returns only an exact receipt from the connection's bound session.

SDK contract and tests are maintained by the SDK worker in this same integration wave. SDK auth occurs first and its query string selects the exact conversation; there is no greeting assumption. Strict capability negotiation prevents accidental task execution on an incompatible server.

## Architecture and durability

`agents/chat_turns.py` tracks lifecycle and subscribers. It calls the existing `spawn_agent_turn`, `_prepare_chat_turn_context` and `_build_chat_turn_runner`; it contains no tool dispatcher or policy exemption. Actual skill execution still belongs to the existing orchestrator and ToolRunner. A trusted internal context carries the exact turn audit and seeds the existing call-context turn ID; model-supplied context cannot mint that audit.

Receipt operations use the existing `MemoryStore` SQLite connection pool and one metadata table. Atomic acceptance binds request UUID, conversation, server turn UUID and a digest of the exact text/context/attachment terms. Duplicate unchanged requests return the original identity and receipt without executing again. Changed terms conflict. A reconnect that explicitly repeats the same request can subscribe to its active receipt, while only the original owner may abort it. Active subscribers are bounded at eight and terminal notifications are deduplicated per live connection. Persisted terminal results can be retrieved/replayed even when the transport delivery failed.

Startup converts any interrupted accepted/running receipt to `outcome_unknown`; it never reconstructs or re-executes the prompt. Cancellation before the runner's first instruction is settled through a retained done callback, so it cannot leave an accepted receipt or live-task reference indefinitely. Cancelling queued B does not trigger A's child teardown. A per-turn check at the existing ToolRunner dispatch checkpoints also prevents new tools if a handler catches coroutine cancellation; native generation leases continue to apply independently.

Acceptance certifies durable **request identity and exact-term digest**, not full transcript persistence. Existing episodes, conversations, snapshots and orchestration history retain their own semantics. No duplicate chat history or new autosave strategy was introduced. Displaying, delivering, reading and seeing a message remain distinct future events; this receipt certifies none of them.

Explicit bounds: terms reject above 8 MiB rather than truncate; sessions use the existing 1024-character bound and reject leading/trailing whitespace and control characters in tracked identity; request and turn identifiers must be canonical UUIDs. At most eight active requests per session and 128 globally are accepted. Retained receipts default to 100,000, configurable through `FERAL_CHAT_TURN_RECEIPT_LIMIT` from 1 to 1,000,000. Reaching capacity returns `chat_turn_quota`; raising the configured limit restores acceptance without deleting deduplication evidence. This card never silently deletes receipts to allow an old task to execute again. A dedicated archive/tombstone lifecycle remains future work.

Structured orchestrator hooks observe pending policy results, plan/policy refusal, provider failure and budget limits. Classification does not guess from assistant wording. Missing runtime or an empty turn has `unavailable`; a possibly interrupted running action has an unknown outcome. Approval notifications remain progress, and their terminal receipt is `awaiting_approval` rather than effect success.

## Tests and reproduction

Real local SQLite and the production `/v1/session` ASGI handler run with real orchestration methods and controlled providers/executors. No account, purchase, message, browser checkout, sensor or external engine is exercised. The lightweight test application omits full boot lifespan; it is a route/dispatcher/storage test, not a installed-app acceptance claim.

Coverage includes read-only capability negotiation before a prompt; first-auth, query-bound session and forged payload session; multi-round provider prose and tool results before one terminal; genuine pending policy gate with no executor call; error, budget, refusal and absent runtime; responsive gateway abort while a provider waits; exact owner/session/request checks; queued B isolation in both command wrappers; cancellation before scheduling and caught-cancel dispatch refusal; duplicate active reconnect subscription and atomic competing claims; durable replay after reconnect; changed-term conflict; acceptance/terminal commit failure; reopened SQLite recovery; explicit size and capacity bounds.

Run from `feral-core`, setting the disposable profile before collection:

```sh
../.venv/bin/python -c 'import os,tempfile,pytest; p=tempfile.TemporaryDirectory(prefix="feral-core04-final-"); os.environ["FERAL_HOME"]=p.name; os.environ["FERAL_DATA_HOME"]=p.name; c=pytest.main(["tests/test_chat_turn_receipts.py","tests/test_chat_turn_abort.py","tests/test_server_websocket.py","tests/test_gateway.py","tests/test_chat_turn_failure_wire.py","tests/test_pending_approval_response.py","tests/test_pending_approval_is_not_failure.py","tests/test_stream_nonstream_parity.py","tests/test_multi_turn_amnesia.py","tests/test_turn_attribution.py","tests/test_agent_turn_lease.py","tests/test_taskflow_dispatch_policy.py","tests/test_tool_runner_exact_approval.py","tests/test_session_auth.py","-q","--no-cov","-p","no:randomly","--tb=short"]); p.cleanup(); raise SystemExit(c)'

../.venv/bin/python -m ruff check agents/chat_turns.py api/server.py gateway/protocol.py models/protocol.py agents/orchestrator.py agents/tool_runner.py memory/store.py api/state.py tests/test_chat_turn_receipts.py tests/test_chat_turn_abort.py --select E,F,W --ignore E501,E402,F401,W291,W293
```

Final result: **226 passed, one pre-existing skipped test and 79 warnings in 7.63 seconds**, exit 0. Warnings concern existing framework deprecations and fixture environment restoration. The skipped server test already describes its pre-existing mocked-send limitation. Targeted Ruff and whitespace checks pass.

## Remaining boundaries

Abort is cooperative once an executor starts. Dispatch checkpoints prohibit later work but cannot retract a committed external effect or stop an executor that ignores cancellation inside its own operation. Such action receipts remain unverified or unknown. A non-cooperative turn can remain active until its own operation terminates; native/global drain contracts are unchanged.

An old socket disconnect follows the existing two-second drain/cancel policy. Reconnect subscription can receive the terminal status of that cancelled original task; it does not grant ownership of the old task or silently restart it. Full phone HUP `chat_request` lifecycle/cancellation is outside this additive desktop/gateway card. The existing native candidate consumes its existing wire protocol and remains staged independently.

If receipt storage fails after work, no durable terminal success is emitted. Status or a restart recovery must establish what is known before any new attempt. Per-owner hosted isolation, full canonical transcript acknowledgements, user read/seen receipts and action-specific verified completion are separate product contracts, not implied by this single-operator turn ledger.
