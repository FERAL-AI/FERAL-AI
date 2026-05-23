---
id: security
title: Security Model
sidebar_position: 4
slug: /guides/security
---

# Security Model

FERAL assumes the LLM is untrusted. Credentials, tool execution, and autonomy are all gated through layered security primitives that prevent prompt injection from escalating into real-world damage.

## BlindVault

The **BlindVault** stores all secrets (API keys, OAuth tokens, database passwords) encrypted at rest in `~/.feral/credentials.enc` (mode `0600`). Encryption is **ChaCha20-Poly1305** (AEAD) with a key derived from your master passphrase via **Argon2id**; the derived key is cached in your OS keychain so the vault unlocks transparently. The LLM never sees raw credential values — the vault injects them at the HTTP layer right before a request leaves the process.

```python
from feral_core.security import BlindVault

vault = BlindVault()
vault.store("weather_api", "sk-abc123...")

# When a skill fires, the vault injects the key:
headers = vault.inject("weather_api", {"X-API-Key": "$CREDENTIAL"})
# headers == {"X-API-Key": "sk-abc123..."}
```

The LLM sees only a placeholder like `[CREDENTIAL:weather_api]` in tool descriptions. Even if the model tries to exfiltrate it, the raw value is never in its context window.

### Vault CLI

```bash
feral vault set OPENAI_API_KEY sk-...
feral vault list
feral vault rotate OPENAI_API_KEY
```

## Permission Tiers

Every tool is tagged with a **PermissionTier** that determines what approval is needed before execution.

| Tier | Auto-execute? | Examples |
|:-----|:--------------|:---------|
| `passive` | Always | Read memory, search web, get weather |
| `active` | In hybrid/loose | Send a message, create a file |
| `privileged` | Only in loose | Run shell command, install package |
| `dangerous` | Never auto | Delete files, send money, modify system config |

Tiers are declared in tool definitions:

```python
from feral_core.security import PermissionTier

@feral_tool(
    description="Delete a file from the filesystem",
    permission=PermissionTier.DANGEROUS,
)
async def delete_file(self, path: str) -> dict:
    ...
```

## ExecutionSandbox

Tools tagged `privileged` or above run inside an **ExecutionSandbox** that constrains what the subprocess can do.

```python
from feral_core.security import ExecutionSandbox

sandbox = ExecutionSandbox(
    allow_network=False,
    allow_fs_write=["/tmp/feral-scratch"],
    max_runtime_seconds=30,
    max_memory_mb=256,
)
result = await sandbox.run(["python3", "untrusted_script.py"])
```

The sandbox uses OS-level isolation (`seccomp` on Linux, `sandbox-exec` on macOS) plus a process timeout. WASM skills get Wasmtime's capability-based sandbox automatically.

## Autonomy Levels

FERAL supports three autonomy modes that control how the PermissionTier system gates execution. See the [Autonomy Levels](./autonomy.md) guide for full details.

| Mode | Behavior |
|:-----|:---------|
| `strict` | Every tool call requires user approval |
| `hybrid` | `passive` + `active` auto-execute; `privileged` + `dangerous` ask first |
| `loose` | Everything except `dangerous` auto-executes |

Set via environment variable or config:

```bash
export FERAL_AUTONOMY=hybrid
```

```json
// ~/.feral/settings.json
{ "autonomy": { "mode": "hybrid" } }
```

## SandboxPolicy Files

For fine-grained control, drop a YAML or JSON policy file in `~/.feral/policies/`:

```yaml
# ~/.feral/policies/production.yaml
name: production
autonomy: hybrid

sandbox:
  allow_network: true
  allow_fs_write:
    - /tmp/feral-scratch
    - ~/.feral/memory.db
  max_runtime_seconds: 60
  max_memory_mb: 512

tool_overrides:
  shell_exec:
    permission: dangerous
  web_search:
    permission: passive
  send_email:
    permission: privileged
    require_confirmation_body: true
```

Load a named policy at startup:

```bash
feral start --policy production
```

Policies are composable — you can layer a base policy with per-session overrides:

```python
from feral_core.security import SandboxPolicy

base = SandboxPolicy.load("production")
session_policy = base.overlay({
    "sandbox": {"allow_network": False},
    "tool_overrides": {"shell_exec": {"permission": "privileged"}},
})
```

## Dangerous-Tool Deny Lists

Even in `loose` mode, certain tools are **always** gated. The `dangerous_tools` surface deny list is hard-coded and cannot be overridden by policy files:

```python
DANGEROUS_TOOLS_DENY_LIST = [
    "delete_all_memory",
    "wipe_database",
    "send_payment",
    "modify_system_files",
    "disable_security",
]
```

You can extend (but never shrink) this list in config:

```json
// ~/.feral/settings.json
{
  "dangerous_tools_extra": [
    "deploy_production",
    "revoke_all_tokens"
  ]
}
```

## enforce_safety

The `enforce_safety()` function runs before every tool execution. It checks:

1. The tool's PermissionTier against the current autonomy level.
2. Whether the tool is on the deny list.
3. Whether a SandboxPolicy restricts the action.
4. Whether a standing approval exists (see [Autonomy Levels](./autonomy.md)).

```python
from feral_core.security import enforce_safety

allowed, reason = await enforce_safety(
    tool_name="shell_exec",
    args={"command": "rm -rf /tmp/old-cache"},
    session=current_session,
)
if not allowed:
    # Ask user for approval, reason explains why
    await request_approval(tool_name, args, reason)
```

If the check fails, the orchestrator pauses execution and surfaces an approval request to the user via the active channel (web UI, CLI, Telegram, etc.).

## Limitations and Caveats

### What FERAL Does NOT Protect Against
- **Physical access attacks**: If someone has physical access to the machine running the Brain, they have access to all data.
- **Supply chain attacks**: Third-party LLM providers can see your prompts (use Ollama for full local processing).
- **Side-channel attacks**: The timing and size of WebSocket messages may reveal information about your activity.
- **Compromised LLM**: If the LLM provider is compromised, tool calls may be manipulated.

### Platform Differences
- **macOS**: Full Accessibility permissions required for desktop automation. Gatekeeper may block unsigned daemons.
- **Linux**: Docker required for code interpreter sandboxing. X11/Wayland differences affect screen capture.
- **Windows**: Limited support. No systemd daemon management.

### Qualified Claims
- Voice latency depends on the provider (OpenAI Realtime ~200ms, Gemini Live ~300ms, local Whisper+Piper ~500ms). FERAL adds ~50ms of WebSocket relay overhead.
- "Local-first" means the Brain runs locally, but cloud LLM providers are used by default. For fully local operation, configure Ollama.
