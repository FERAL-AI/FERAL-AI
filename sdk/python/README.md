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

## Quick start

```python
from feral_sdk import FeralPlugin, feral_tool

class WeatherPlugin(FeralPlugin):
    name = "weather"
    description = "Real-time weather data"

    @feral_tool(description="Get current weather for a city")
    async def current(self, city: str) -> dict:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.weatherapi.com/v1/current.json",
                params={"key": "YOUR_KEY", "q": city},
            )
            return resp.json()
```

### Generating a manifest

```python
plugin = WeatherPlugin()
manifest = plugin.to_manifest()   # dict ready for manifest.json
```

The manifest uses `internal://` URLs with method `PYTHON`, which tells the
Brain's SkillExecutor to resolve the call through the registered Python
implementation rather than making an HTTP request.

### Registering with the Brain

Place your plugin module as `impl.py` inside the skill directory
(`~/.feral/skills/<skill_id>/`) alongside `manifest.json`, or call
`register_instance()` at startup:

```python
from skills.impl import register_instance

plugin = WeatherPlugin()
register_instance(plugin.name, plugin)
```

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

`bearer_token` is caller supplied and used for HTTP only. The SDK does not log
it. Avoid logging request headers or exception request objects in your own code.
WebSocket `chat()` authentication/session/stream handling remains an unverified
legacy interface in this slice; this HTTP work does not certify it for remote
chat. Device credential scopes may differ from the operator's API key; supplying
a credential does not expand server permissions.

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

## Requirements

- Python >= 3.11
- httpx, websockets, pydantic (installed automatically)

## License

Apache-2.0
