# FERAL Agent Capabilities

This is the canonical "how does FERAL actually think and act" reference. If
you've never used FERAL before, read it top-to-bottom; every feature below
links to the file that implements it so you can follow along in the source.

FERAL is intentionally opinionated about one thing: **it must not stall**.
If the user asks for something FERAL can't yet do, it either (a) tells you
clearly what's missing, (b) asks for approval, or (c) writes the capability
itself and uses it — depending on your autonomy tier.

---

## 1. Autonomy Tiers (Strict / Hybrid / Loose)

FERAL has three tiers picked per-device and per-session. They are enforced
in real code, not a config flag, by the orchestrator at every tool-call
decision point.

| Tier | Capability gap behavior | Safe tool call | Unsafe / novel tool call |
|------|-------------------------|----------------|--------------------------|
| **Strict** | Refuse, tell the user exactly what's missing, suggest `feral install` or `feral approve`. | Runs after explicit approval card. | Always requires an approval card. |
| **Hybrid** (default) | Draft a new tool via Tool Genesis, run it in the sandbox, and *ask* before promoting. | Auto-executes. | Approval card. |
| **Loose** | Draft, sandbox, promote, and re-dispatch the user's request **in the same turn**. You review the log afterwards. | Auto-executes. | Auto-executes unless classified unsafe. |

**Implementation:**

- Gap detection and tier fork: `feral-core/agents/orchestrator.py`
  (`_on_capability_gap`, called whenever no existing skill matches an intent).
- Tier enforcement and approval cards: `feral-core/security/` (approval manager)
  and the safety classifier used by `feral-core/agents/tool_runner.py`.
- Self-reported tier in every system prompt: `feral-core/agents/self_model.py`
  (the `Runtime: … autonomy=<tier>` line).

---

## 2. Tool Genesis — the Capability Autopilot

When FERAL encounters a task it has no skill for, the **Tool Genesis** engine
turns that into a new tool, end-to-end, without a human in the loop (unless
the tier demands one).

Pipeline (see `feral-core/agents/tool_genesis.py`):

1. **Gap signal.** `orchestrator._on_capability_gap(intent, context)` fires.
2. **Draft.** The engine asks the planner LLM for a Python tool that
   satisfies the intent; the draft is an AST-validated, import-allowlisted
   `GenesisTool` object.
3. **Sandbox.** The tool runs in the Docker sandbox
   (`--network=none --memory=512m --cpus=1 --read-only`) against a synthetic
   input. Failures feed back into a repair loop.
4. **Promote.** `ToolGenesisEngine.promote()` converts the passing tool into
   a real skill manifest via `GenesisTool.to_skill_manifest()` and persists
   it under `~/.feral/skills/` so it survives restarts.
5. **Dispatch.** Control returns to the orchestrator, which now *does* have
   a matching skill and calls it immediately.

### Worked example

> **User:** "Convert `/tmp/sales.csv` into JSON and save it next to it."
>
> FERAL has no `csv_to_json` skill.

On `loose`:

1. `_on_capability_gap` fires with intent = `"convert csv to json"`.
2. Genesis drafts a tool `csv_to_json(path, output=None)` that uses the
   stdlib `csv` + `json` modules only (the import allowlist rejects anything
   else).
3. Sandbox invocation with a synthetic 3-row CSV succeeds, producing valid
   JSON.
4. `promote()` writes a new manifest, registers the skill, and logs a
   Glass Brain event.
5. The orchestrator re-dispatches the original turn; the user sees one reply:
   `"Wrote /tmp/sales.json (24 rows)."` — same turn, zero stalls.

On `hybrid`, step 4 pauses to show an approval card describing the proposed
skill, the imports used, and the sandbox result. You click once; 5 proceeds.

On `strict`, the answer is: `"I don't have a csv_to_json skill. Install one
from registry.feral.sh or run feral approve tool-genesis to let me build it."`

**HTTP surface:**

- `POST /api/tool-genesis/execute` — run an in-memory draft against the
  sandbox.
- `POST /api/tool-genesis/approve` — promote a pending draft.
- `GET  /api/tool-genesis/pending` — enumerate drafts awaiting approval.
- `DELETE /api/tool-genesis/{id}` — drop a draft.

(See router registration inside `feral-core/api/` — the `tool_genesis` router.)

---

## 3. Agent Mitosis — Specialists That Inherit Narrow Permissions

Some tasks recur often enough that spawning a dedicated specialist is worth
it. **Agent Mitosis** (see `feral-core/agents/agent_mitosis.py`) lets the
main orchestrator either route *this* turn to a specialist, or synthesize a
new specialist from Tool Genesis patterns.

Two entry points on the orchestrator
(`feral-core/agents/orchestrator.py`):

- `handle_command(...)` and `handle_command_stream(...)` both call
  `route_to_specialist(intent)` before doing their own planning. If a
  specialist exists that better matches the intent, the turn is handed off.
- When Tool Genesis sees the same intent shape repeat, it calls
  `propose_specialist()` to spawn a new specialist with a narrowed tool set.

**Narrow-by-default permissions:** a specialist is not the full agent. It
inherits:

- A subset of the parent's skill manifests (only the tools its system prompt
  and historical invocations needed).
- The parent's autonomy tier (never higher).
- A derived system prompt with the specialist's niche + `self_model.py`'s
  standard Runtime/Tooling sections.

This matters because a "finance specialist" spawned from a tax-question
pattern cannot, e.g., flip smart-home lights or send Telegram messages; it
simply doesn't see those tools. Security-by-omission.

---

## 4. Workspace Scripts — the Never-Say-No Escape Hatch

`feral-core/skills/impl/workspace_scripts.py` is the catalog-backed skill
that keeps FERAL from refusing requests that don't match any other skill.

What it does:

- Exposes a curated set of user-authored scripts under a workspace root
  (typically `~/.feral/workspace/scripts/`) as first-class tool calls.
- Each script declares its name, description, inputs, and safety class in a
  simple manifest so the LLM sees it the same way it sees a built-in skill.
- The orchestrator always surfaces it in its `ALWAYS_INCLUDE` list so the
  model knows this fallback path exists every turn.

**Typical catalog entries** (installed by default):

- `run_python(code)` — one-shot Python against the sandbox.
- `run_shell(command)` — shell inside the allowlist-checked sandbox.
- `fetch_url(url)` — HTTP GET with safety filter.
- `read_file(path)` / `write_file(path, content)` — workspace-scoped I/O.

Users can drop new scripts into `~/.feral/workspace/scripts/`; they are
picked up without a restart. This is the "I don't have a skill for that *but
I definitely have a general-purpose scripting hole*" path, and is exactly
why FERAL doesn't stall.

---

## 5. Retry Mechanics — Why FERAL Doesn't Wait Forever

LLMs sometimes respond with *intent but no action*: "Sure, I'll do that
now." With zero tool calls attached. Or they produce an empty response. Or
they produce reasoning-only output mid-stream.

FERAL's retry machinery (`feral-core/agents/refusal_handler.py`, plus retry
hooks in `feral-core/agents/orchestrator.py`) catches each pattern:

| Pattern | What FERAL does |
|---------|-----------------|
| **Reasoning-only** (chain-of-thought shipped, no final tool call) | Re-prompt with an injected "you must emit a tool call or a final answer — choose one" addition, without polluting persisted history. |
| **Empty response** | Retry once with a stricter temperature and the same prompt-addition hint. |
| **Ack-execution fast path** ("I'll do that now.") | Detect the ack shape, inject an addition that *forbids* bare acknowledgements, and retry immediately. |

Key design choices:

- **Retries never mutate the persisted conversation.** The prompt-addition
  is attached only for the retry call and discarded afterwards, so memory
  stays clean.
- Retries are bounded (default 2 per turn) and tracked in metrics as
  `feral.retry.{pattern}` counters so you can tune the model or the prompt.
- `ALWAYS_INCLUDE` now covers `messaging_channels`, `self_introspection`,
  `workspace_scripts`, `coding_tools`, and `computer_use` so the model
  literally cannot "forget" these fallbacks exist.

The net effect: FERAL has a pathological resistance to dead-air turns.

---

## 6. Browsing, Publishing, and Installing from registry.feral.sh

`registry.feral.sh` is the community marketplace for skills, tools, and
hardware node manifests. The service is in `feral-registry/`
(FastAPI + Postgres + Ed25519 signing).

### Browse

- Web UI lists items by type (`skill`, `tool`, `node`) with tags,
  downloads, and flags.
- Read-only JSON API at `GET /items` and `GET /items/{id}` for programmatic
  discovery.

### Publish

```bash
# from your feral-core/skills/my_new_skill/ folder
feral publish ./my_new_skill
```

Under the hood (`feral-core/cli/publish.py`):

1. Packs the skill folder into a tarball.
2. Uploads via `POST /items` using your GitHub OAuth token (the registry
   relies on GitHub OAuth for identity — see
   `feral-registry/feral_registry/auth.py`).
3. The registry signs the bundle with its Ed25519 key
   (`feral-registry/feral_registry/signing.py`) and returns a signed
   manifest. Every published bundle is signed; unsigned bundles refuse to
   install.

### Install

```bash
feral install <name>          # the skill_id, e.g. `feral install web_search`
feral install <registry_uuid> # also resolves; nobody types this
```

`feral-core/cli/install.py`:

1. Pulls the signed bundle from the registry.
2. Verifies the signature against the registry's public key shipped with
   the client.
3. Drops the skill into `~/.feral/skills/` and reloads the skill registry
   in place — no brain restart required.

### Flag

Anyone can `POST /flag` against an item (abuse, malware, broken). Flagged
items are hidden from the catalog until a maintainer reviews them.

---

## 7. Building a Hardware Daemon Against HUP

HUP — the **Hardware Unification Protocol** — is the wire spec every node
speaks to the brain. Canonical doc: `feral-nodes/HUP_SPEC.md`.

The fastest path to a new device:

### Python

1. Copy the template:

   ```bash
   cookiecutter feral-nodes/templates/hardware-daemon/
   ```

2. Implement `read_sample()` and `apply_command(cmd)` against your sensor or
   actuator. The `FeralNode` base class (`feral-nodes/python-node-sdk/src/`)
   handles the handshake, heartbeat, reconnection, and framing.
3. Run it:

   ```bash
   python -m my_daemon --brain ws://localhost:9090 --api-key $FERAL_API_KEY
   ```

The daemon advertises itself over mDNS; the brain discovers, pairs (via QR
or approval card), and starts receiving frames.

### TypeScript

Same story via `feral-nodes/ts-node-sdk/` — ship the node as npm package
`@feral-ai/node-sdk`, wire the same `read_sample` / `apply_command`
contract, and point it at the brain.

### Existing reference daemons

- `feral-nodes/python-node-sdk/hardware_daemon/` — BLE wristband HR/SpO2
  sampler.
- `feral-nodes/python-node-sdk/w300_daemon.py` — smart-glasses video
  bridge.
- `feral-nodes/python-node-sdk/robot_template.py` — serial / ROS robot
  starter. It refuses every actuator command until you implement its
  `RobotTransport`, and verifies each command against a telemetry
  read-back before reporting success. Both are deliberate: copy them.

Use them as the shape of your own daemon.

---

## Further reading

- Architecture overview: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).
- Adding a skill by hand (no Tool Genesis): [`docs/ADDING_SKILLS.md`](ADDING_SKILLS.md).
- Hardware ecosystem: [`docs/HARDWARE_ECOSYSTEM.md`](HARDWARE_ECOSYSTEM.md).
