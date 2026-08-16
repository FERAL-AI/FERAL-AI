---
id: skills
title: Writing Skills
sidebar_position: 1
slug: /guides/skills
---

# Writing Skills

A **skill** teaches FERAL a new capability — calling an API, transforming data, or controlling hardware. There are three ways to create one, depending on complexity.

## Option 1: JSON Manifest (any REST API)

Drop a JSON file in `~/.feral/skills/`. The Brain discovers it at startup and exposes the endpoints as agent tools.

```json
{
  "skill_id": "weather_api",
  "version": "1.0.0",
  "description": "Real-time weather data for any city",
  "brand": {
    "name": "Weather",
    "icon": "cloud",
    "primary_color": "#3b82f6"
  },
  "auth": {
    "type": "api_key",
    "api_key_header": "X-API-Key"
  },
  "endpoints": [
    {
      "id": "current",
      "method": "GET",
      "url": "https://api.weather.example/current",
      "description": "Get current weather for a city. The LLM reads this to decide when to use the tool.",
      "params": [
        { "name": "city", "type": "string", "required": true, "description": "City name" },
        { "name": "units", "type": "string", "required": false, "description": "celsius or fahrenheit" }
      ]
    },
    {
      "id": "forecast",
      "method": "GET",
      "url": "https://api.weather.example/forecast",
      "description": "Get a 5-day forecast",
      "params": [
        { "name": "city", "type": "string", "required": true }
      ]
    }
  ],
  "trigger_phrases": ["weather", "temperature", "forecast"],
  "categories": ["weather", "utility"]
}
```

Set the API key securely:

```bash
export FERAL_KEY_weather_api=your-key-here
```

The Blind Vault stores this key and injects it into requests — the LLM never sees the raw value.

### Manifest Fields

| Field | Required | Description |
|:------|:---------|:------------|
| `skill_id` | yes | Unique identifier |
| `description` | yes | The LLM reads this to decide when to invoke the skill |
| `brand` | no | Display name, icon, and color in the dashboard |
| `auth` | no | Authentication config (`api_key`, `bearer`, `oauth2`) |
| `endpoints[]` | yes | One or more callable endpoints |
| `trigger_phrases` | no | Hints for the LLM's skill selection |
| `categories` | no | For marketplace and dashboard grouping |

## Option 2: Python Plugin

For tools that need custom logic (not just HTTP calls), use the SDK's `FeralPlugin` base class.

```python
from feral_sdk import FeralPlugin, feral_tool

class TranslatorPlugin(FeralPlugin):
    name = "translator"
    description = "Translate text between languages"
    version = "0.1.0"

    @feral_tool(description="Translate text to a target language")
    async def translate(self, text: str, target_lang: str) -> dict:
        # Custom logic — call a translation API, run a local model, etc.
        translated = await self._call_translation_service(text, target_lang)
        return {"original": text, "translated": translated, "language": target_lang}

    async def _call_translation_service(self, text: str, lang: str) -> str:
        # Your implementation
        ...

    async def on_load(self):
        """Called once when the Brain loads this plugin."""
        print(f"Translator plugin v{self.version} loaded")
```

### Plugin Lifecycle

| Method | When |
|:-------|:-----|
| `on_load()` | Brain starts or hot-reloads the plugin |
| `on_unload()` | Brain shuts down |
| `execute(endpoint_id, args, vault)` | Brain invokes a tool (called automatically) |
| `to_manifest()` | Generates a skill manifest from `@feral_tool` decorators |

### Installing a Python Plugin

Place the module in `~/.feral/plugins/` or install it as a Python package. The Brain discovers plugins that subclass `FeralPlugin`.

## Option 3: WASM Skill (sandboxed)

For untrusted or third-party skills, FERAL can execute WebAssembly modules in a Wasmtime sandbox.

```bash
# Scaffold a new WASM skill
cp -r templates/wasm-skill-assemblyscript my-skill
cd my-skill
npm install
npm run build    # produces build/skill.wasm
```

Place the `.wasm` file alongside a manifest:

```json
{
  "skill_id": "my_wasm_skill",
  "description": "Sandboxed skill",
  "runtime": "wasm",
  "wasm_path": "skill.wasm",
  "endpoints": [
    { "id": "run", "description": "Execute the skill" }
  ]
}
```

## Self-Learning Skills

FERAL can auto-detect repeated patterns and generate skill manifests from usage history. When the agent notices it keeps performing the same multi-step sequence, it proposes a new skill. You can review and approve pending skills:

```bash
# List pending auto-generated skills
curl http://localhost:9090/api/skills/pending

# Approve one (registers it live and writes it to ~/.feral/skills/<id>/)
curl -X POST http://localhost:9090/api/skills/approve \
  -H "Content-Type: application/json" \
  -d '{"skill_id": "my_skill"}'

# Discard one
curl -X POST http://localhost:9090/api/skills/reject \
  -H "Content-Type: application/json" \
  -d '{"skill_id": "my_skill"}'
```

Both take the id in a JSON body, not in the path.

### What a failure looks like

The approval queue lives in memory and a brain restart empties it, so
"approve" and "reject" both have a common failure: the id is no longer
queued. Neither call reports that as a success.

| Outcome | Status | Body |
|---|---|---|
| Approved and registered | 200 | `{"ok": true, "skill_id": "...", "registered": true}` |
| Rejected | 200 | `{"ok": true, "skill_id": "...", "rejected": true}` |
| Id is not in the approval queue | 409 | `{"ok": false, "code": "not_pending", "error": "..."}` |
| Was queued, and the brain failed to register or persist it | 500 | `{"ok": false, "code": "registration_failed", "error": "..."}` |
| No skill generator on this brain | 503 | `{"ok": false, "error": "..."}` |
| `skill_id` missing from the body | 400 | `{"ok": false, "error": "skill_id is required"}` |

Rejecting an id the brain does not have is an error, not an idempotent
no-op: the usual cause is a UI left open across a restart, and a green
tick there would say a draft was discarded when nothing was. Every
failure carries both a non-2xx status and an `error` string, so a generic
client sees it without knowing this route's body shape.

`GET /skills` follows the same rule: it answers 503 with an `error` when
the skill registry is not initialized, rather than an empty list that
reads as "no skills installed".

## Skill Marketplace

Browse and install community skills from the registry:

```bash
feral marketplace search "weather"
feral marketplace install weather_api
```

Or via the API. Installing is two calls: `preview` verifies the
publisher's signature and returns what the skill can reach plus a
single-use `install_token`, and `install` spends that token. A request
without a valid token is refused with 403, so nothing is installed
before its permissions have been served to whoever is deciding.

```bash
curl "http://localhost:9090/api/marketplace/search?q=weather"

# 1. Verify + describe. Nothing is written to disk.
curl -X POST http://localhost:9090/api/marketplace/preview \
  -H "Content-Type: application/json" \
  -d '{"kind": "skill", "id": "weather_api"}'
# -> {"success": true, "permissions": ["network"],
#     "permission_details": [{"id": "network", "label": "Internet access", ...}],
#     "signature": {"verified": true, "sha256": "..."},
#     "install_token": "..."}

# 2. Install exactly that package.
curl -X POST http://localhost:9090/api/marketplace/install \
  -H "Content-Type: application/json" \
  -d '{"kind": "skill", "id": "weather_api", "install_token": "..."}'
```

The token is bound to the item id and to the SHA-256 of the verified
bundle, is good for one install, and expires after five minutes. A
bundle whose signature does not verify never gets one.
