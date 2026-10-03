# feral-sdk (Python)

Python SDK for building FERAL plugins, tools, device adapters, and GenUI components.

## Installation

```bash
pip install feral-sdk
```

Or install from source during development:

```bash
pip install -e sdk/python
```

## Author a Python tool package

`feral-sdk` imports independently of the Brain. The runtime interpreter must
also have `feral-sdk` installed before loading a Python SDK plugin; the package
installer does not install dependencies. Availability inside a shipped native
bundle is a separate packaging gate.

```python
# impl.py in your own authored skill package
from __future__ import annotations
from feral_sdk import FeralPlugin, feral_tool

class Calculator(FeralPlugin):
    name = "my_calculator"
    description = "Local integer arithmetic"

    @feral_tool(description="Add two integers")
    async def add(self, a: int, b: int) -> dict:
        return {"sum": a + b}

# Explicit module-level export discovered by the existing BaseSkill loader.
CalculatorSkill = Calculator.runtime_skill()
```

Write `Calculator().to_manifest()` as JSON to `manifest.json` beside `impl.py`.
The generated endpoints use method `PYTHON` and an empty URL; they require review
by default. No `internal://` transport or automatic plugin registration exists.
`runtime_skill()` imports the runtime lazily and gives an explicit error when
`skills.base` is absent. SDK import and manifest generation do not require it.

Type hints, including postponed annotations, infer scalar/list/dict parameter
kinds. Unsupported or unresolved hints require explicit `parameters=`. Defaults
are copied into the manifest. Tools must be asynchronous, with distinct bounded
identifiers; malformed parameter definitions fail before installation. This
validation is not a sandbox for arbitrary plugin code. Trust the author and keep
blocking operations off the event loop. `on_load`/`on_unload` are not called by
the current dynamic loader.

Install the package using the existing runtime's package contract, then request
`POST /api/skills/reload?skill_id=my_calculator` with the deployment's
credentials. Review and invoke through FERAL's existing tool path. Do not use a
direct Python handler call as proof of FERAL authorization.

Installed Python packages must export one unique BaseSkill adapter with the
matching skill ID and an asynchronous implementation. A bare FeralPlugin
subclass is insufficient. Reload prepares the replacement before publication;
an invalid package or changed source preserves the working implementation.
Cancellation or registry replacement prevents a late candidate from publishing.
Trusted Python imports are not sandboxed or rolled back. Shipped-manifest
inventory refresh is identified separately from installed-package replacement;
neither acknowledgement proves account availability or a verified tool outcome.
The executable [SDK authoring walkthrough](../../examples/sdk-authoring/README.md)
checks installation, the actual registered reload route, actual SkillExecutor,
Deny, foreign/reused reviews and uninstall in a disposable fresh process. Its
negative probe requires an unusable bare plugin to be refused without activation.

## Key modules

| Module | Purpose |
|--------|---------|
| `feral_sdk.plugin` | `FeralPlugin` base class |
| `feral_sdk.tool` | `@feral_tool` decorator |
| `feral_sdk.client` | `FeralClient` for talking to a running Brain |
| `feral_sdk.device` | `HUPDevice` base for hardware adapters |
| `feral_sdk.manifest` | `SkillManifest`, `Endpoint`, `Parameter` dataclasses |
| `feral_sdk.genui` | GenUI component helpers |

## HTTP client

The client uses the running Brain's registered HTTP routes and its existing
authorization/policy gates. It does not start a Brain, discover credentials,
connect accounts, grant permissions or retry actions.

```python
import asyncio
import os
from feral_sdk import FeralClient

async def main():
    # Optional for trusted loopback HTTP; required off-loopback according to
    # the server's policy. Use HTTPS for a remote deployment.
    async with FeralClient(
        "http://localhost:9090",
        bearer_token=os.environ.get("FERAL_API_KEY"),
        timeout=30,
    ) as client:
        health = await client.health()
        # Reachable does not imply memory, credentials or the agent is ready.
        print(health.get("agent_ready"))
        skills = await client.list_skills()
        result = await client.invoke_skill(
            "notes_memory", "search_notes", {"query": "project"},
            session_id="my-project-thread",
        )
        if result.get("success") is True:
            print(result.get("data"))
        else:
            # Render a refusal/review state; do not treat HTTP 200 as execution.
            print(result.get("status_code"), result.get("error"))

asyncio.run(main())
```

`health()` requests `/health`, and `list_skills()` requests `/skills` with
`Accept: application/json`; those paths also serve dashboard navigation.
`invoke_skill()` posts `{skill_id, endpoint, args, confirm}` to
`/api/tools/execute`, adding `session_id` only when supplied. Pass the active
thread/session identity explicitly when it matters for plan mode, approval
context or coding continuity. Omitting it does not create a session.

`confirm=False` is the default. Only pass `confirm=True` after the caller has
reviewed and authorized the exact action; it is an explicit assertion, not an
SDK approval dialog. The server may still refuse execution through its other
gates. The client never changes a denial to confirmation or retries a request.

`create_note(content, tags, session_id=..., confirm=False)` invokes the existing
`notes_memory/save_note` skill through that same policy surface. It returns the
tool envelope, including `success`, `status_code`, `data` and `error`; the saved
note is in `data` on success. The former `/api/notes` URL was not registered.

Failure semantics:

- Non-success HTTP responses (including redirects) raise
  `httpx.HTTPStatusError` before parsing. Redirects are not followed.
- Timeouts/connection failures propagate as `httpx` transport exceptions. The
  default timeout is 30 seconds; callers may supply a float or `httpx.Timeout`.
  An uncertain result requires reconciliation before another effectful request.
- Invalid JSON or a wrong object/list shape raises `ValueError` with a bounded
  diagnostic that excludes the response body. Missing list fields do not silently
  become empty lists.
- HTTP 200 tool refusals remain returned dictionaries: for example
  `success=False, status_code=412` means review is required, and 403 means policy
  denial. Inspect the envelope before marking an action executed. This is distinct
  from an HTTP transport/status failure.
- `search_memory()` returns the `results` list, preserving its existing SDK
  return type. To inspect tier-degradation metadata, consume the full
  `/api/memory/search` envelope directly; this convenience method does not expose it.

`bearer_token` is caller supplied. HTTP sends it in the Authorization header;
WebSocket chat sends it in the first authentication frame. Chat does not put
credentials in URLs or log frames; its transport debug logger is disabled.
Avoid logging HTTP request headers or exception request objects in your own code.
Device credential scopes may differ from the operator's API key; supplying a
credential does not expand server permissions. Session WebSocket authentication
accepts the server's operator API key or valid session credential; an HTTP-only
device credential is not automatically a chat credential.

## Tracked chat turns

```python
from feral_sdk import FeralClient, TurnProcessingOutcome

async def chat():
    async with FeralClient(session_id="my-project-thread", chat_timeout=60) as client:
        receipt = await client.chat_turn("Inspect the project and summarize your findings")
        if receipt.processing_outcome == TurnProcessingOutcome.AWAITING_APPROVAL:
            print("Review required:", receipt.approval_request_ids)
        print(receipt.processing_outcome, receipt.final_text)
        # Processing completed does not prove every requested external effect.
        print(receipt.action_outcome)  # not_asserted or unknown
```

The client creates an isolated UUID thread identity by default and keeps that ID
across calls on that client. Supply `session_id` on the constructor or method to choose an
existing thread. Independent clients have independent default threads. Concurrent
calls for the same thread on one client raise `ChatTurnError(code="session_busy")`;
separate explicit threads can run independently. Sharing the same explicit thread
across other surfaces remains subject to the server's attachment/delivery rules.
Each thread now retains its negotiated socket and one owned reader between
calls. Later turns reuse the live server history instead of disconnecting after
every response. This is live continuity: closing the last nonprimary attachment
still clears the server's volatile context. Saved UI messages and durable turn
receipts are separate from model history; process-restart durability remains a
backend follow-up. A new client attaching an old explicit ID cannot certify that
its earlier context survived.

Set `max_chat_threads=8` on the client (default 8; integer 1 through 64) to bound
live threads. `thread_quota` refuses a new thread without evicting existing
history. `await client.close_thread(session_id)` releases that owned thread;
omitting the ID closes the default thread. Explicit close, idle disconnect,
malformed protocol, timeout or cancellation retires the old ID on that client.
A later prompt to that ID raises `ChatTurnError(code="context_lost")` before any
reconnect or user command. The SDK does not silently start an empty conversation
under a previously used ID. Choose a **new** explicit ID for deliberate new
work, after reconciling any uncertain earlier action; this does not resume the
old task. Up to 1,024 live/retired identities are recorded per client, after which
`thread_identity_quota` refuses further IDs. There is no automatic eviction or
reset of those identity records. `close()` permanently closes the client;
further chat raises `client_closed`.

```python
async with FeralClient(session_id="thread-A", max_chat_threads=4) as client:
    await client.chat("Remember violet maple 47 for this conversation")
    answer = await client.chat("What did I ask you to remember?")
    await client.close_thread()  # Releases A; it cannot silently reopen.
    fresh = await client.chat("Start a separate conversation", session_id="thread-B")
```

Chat authenticates first, then sends a read-only `chat.capabilities` request.
Only a correlated response confirming version 1, durable receipts, whole-turn
terminals and the selected session permits sending the user command. An older or
unsupported server therefore receives no task prompt. There is no greeting
dependency or automatic response-only downgrade.

`chat_turn()` requires `chat_turn_accepted` followed by the exact correlated
`chat_turn_terminal` request/session/server-turn IDs. An approval notification,
`text_response`, or model-round `stream_delta.is_final` never resolves the call.
It returns a frozen `ChatTurnReceipt` for completed, awaiting_approval, failed,
cancelled, outcome_unknown, unavailable, refused or budget_exceeded processing.
Inspect that outcome and approval IDs; the SDK never confirms a review for you.

`chat()` is a string convenience method. It returns nonempty final prose only
after a `completed` processing receipt. Other terminal outcomes raise
`ChatTurnError`, carrying the receipt. This describes response processing, not
verified purchase, delivery or other task effects. Use the application's actual
effect receipt when those outcomes matter.

The total deadline includes connection, authentication, capability negotiation
and execution. Set `chat_timeout` on the client or `timeout=` on a chat call.
It raises `ChatTurnTimeout`; partial prose is never successful output. Invalid
frames, rejected negotiation, authentication denial or an early connection close
raise bounded `ChatTurnError` diagnostics without server bodies or credentials.
Correlated server error frames are failures, not fabricated assistant replies.

Cancelling the Python coroutine or calling `close()` closes only SDK-owned
transports. This does not assert that server work or prior external effects were
cancelled. The SDK does not send an abort, reconnect, replay a task or retry an
uncertain result. Reconcile through the server's turn/effect receipts before
another effectful invocation. A default UUID thread is not a multi-user identity
or a new permission grant.

## HTTP contract checks

From the repository root with the pinned development environment:

```sh
.venv/bin/python -m pytest sdk/python/tests -q --no-cov -p no:randomly
```

The suite uses actual registered server handlers, the API-key middleware and
tool policy/context binding through an in-process ASGI transport, with disposable
state and a recording executor. It also checks transport/schema failures. It
does not start the real Brain lifespan, use personal profiles or test a network
deployment. See [dated evidence](../../docs/roadmap/theora-personal-agent/PYTHON_SDK_EVIDENCE.md).
Tracked-chat tests additionally bridge to the actual registered session handler,
real SQLite turn receipts and a controlled real orchestrator, with disposable
homes and in-memory OS-vault wrappers. No real model or account is used. See
[tracked SDK evidence](../../docs/roadmap/theora-personal-agent/SDK_WEBSOCKET_EVIDENCE.md)
and [live thread evidence](../../docs/roadmap/theora-personal-agent/SDK_THREAD_CONTINUITY_EVIDENCE.md).

## Requirements

- Python >= 3.11
- httpx, websockets, pydantic (installed automatically)

## License

Apache-2.0
