---
id: autonomy
title: Autonomy Levels
sidebar_position: 8
slug: /guides/autonomy
---

# Autonomy Levels

FERAL's autonomy system controls how much freedom the agent has to act without human approval. Three modes — **strict**, **hybrid**, and **loose** — determine which tools auto-execute and which require explicit confirmation.

## Strict Mode

Every tool call requires user approval. The agent proposes an action and waits.

```
User: "What's the weather in NYC?"
Agent: I'd like to call the weather API for NYC. Approve? [✓ / ✗]
User: ✓
Agent: It's 72°F and sunny in New York.
```

Best for: first-time setup, untrusted environments, auditing all agent behavior.

```json
// ~/.feral/settings.json
{ "autonomy": { "mode": "strict" } }
```

## Hybrid Mode

Safe actions (`passive` + `active` permission tiers) execute automatically. Risky actions (`privileged` + `dangerous`) require approval.

```
User: "Search for flights to Tokyo and book the cheapest one."
Agent: [auto] Searching flights... found 3 options.
Agent: The cheapest is $450 on ANA. I need approval to book. Approve? [✓ / ✗]
```

This is the **default** mode. Most users stay here — the agent handles research and information retrieval instantly, but pauses before actions with real-world consequences.

```yaml
autonomy: hybrid
```

### What Auto-Executes in Hybrid

| Action | Tier | Auto? |
|:-------|:-----|:------|
| Search the web | passive | Yes |
| Read memory | passive | Yes |
| Get weather | passive | Yes |
| Send a chat message | active | Yes |
| Create a file | active | Yes |
| Run a shell command | privileged | No — asks first |
| Send an email | privileged | No — asks first |
| Delete files | dangerous | No — asks first |
| Make a purchase | dangerous | No — asks first |

## Loose Mode

Everything except `dangerous`-tier tools auto-executes. The agent runs on full autopilot for most tasks.

```
User: "Clean up my Downloads folder — delete anything older than 30 days."
Agent: [auto] Listing files... found 47 files older than 30 days.
Agent: [auto] Moving 47 files to trash... done.
```

Note: `dangerous` tools still require approval even in loose mode. See [Security Model](./security.md) for the hard-coded deny list.

```yaml
autonomy: loose
```

## Configuration

### Environment Variable

```bash
export FERAL_AUTONOMY=hybrid   # strict | hybrid | loose
```

The env var takes precedence over the config file, useful for per-session overrides:

```bash
FERAL_AUTONOMY=strict feral start
```

### Config File

```json
// ~/.feral/settings.json
{ "autonomy": { "mode": "hybrid" } }
```

### Per-Session Override

Clients can request an autonomy level during session initialization:

```python
from feral_sdk import FeralClient

async with FeralClient("http://localhost:9090") as client:
    session = await client.create_session(autonomy="strict")
```

The server enforces that a session cannot escalate beyond the server-level setting. If the server is `hybrid`, a session can request `strict` (more restrictive) but not `loose` (less restrictive).

## ApprovalManager

The `ApprovalManager` handles approval requests and maintains a list of **standing approvals** — pre-authorized tool+argument patterns that skip the approval prompt.

```python
from feral_core.security import ApprovalManager

manager = ApprovalManager()

# Grant a standing approval
manager.grant_standing(
    tool="shell_exec",
    pattern={"command": "ls *"},     # glob match on args
    expires_in=3600,                  # seconds; None = permanent
)

# Check if a tool call is pre-approved
approved = manager.check("shell_exec", {"command": "ls -la ~/Documents"})
# approved == True (matches "ls *" pattern)
```

### Standing Approvals via CLI

```bash
# Grant: allow "git status" without asking
feral approve "shell_exec" --pattern '{"command": "git *"}' --expires 1h

# List active standing approvals
feral approve list

# Revoke all standing approvals
feral approve revoke --all
```

### Standing Approvals via API

```bash
# Grant
curl -X POST http://localhost:9090/api/approvals \
  -H "Content-Type: application/json" \
  -d '{"tool": "shell_exec", "pattern": {"command": "git *"}, "expires_in": 3600}'

# List
curl http://localhost:9090/api/approvals
```

## Approval Flow

When a tool call needs approval, the following sequence occurs:

1. `enforce_safety()` determines the call requires approval.
2. The Brain sends an `approval_request` message to the active channel.
3. The user responds with approve or deny.
4. If approved, the tool executes and the result flows back normally.
5. If denied, the agent receives a "tool denied" signal and re-plans.

```json
{
  "type": "approval_request",
  "payload": {
    "request_id": "apr_abc123",
    "tool": "shell_exec",
    "args": {"command": "rm -rf /tmp/old-cache"},
    "permission_tier": "privileged",
    "reason": "Shell command execution requires approval in hybrid mode"
  }
}
```

## Escalation Rules

| Server Setting | Session Request | Effective Level |
|:---------------|:----------------|:----------------|
| `strict` | `strict` | strict |
| `strict` | `hybrid` | strict (capped) |
| `hybrid` | `strict` | strict |
| `hybrid` | `hybrid` | hybrid |
| `hybrid` | `loose` | hybrid (capped) |
| `loose` | any | as requested |

## Spend Limits

Autonomy decides whether FERAL asks before acting. Spend limits decide
whether it may act **at all** where money is involved, and they are
checked before you are ever asked to approve. An amount over the limit
is refused, not offered: otherwise the answer to "approve?" would be
the thing that breaks the limit.

**Nothing works until you set them.** A fresh install ships zeros, and
zero means refuse:

```json
{
  "commerce": {
    "currency": "USD",
    "per_transaction_max": "0",
    "per_day_max": "0",
    "merchant_allowlist": []
  }
}
```

With those defaults every purchase flow is refused with
`reason: "no_cap_configured"`. That is deliberate. "No limit
configured" must never be read as "no limit", so an install nobody has
configured cannot spend. Set both caps in `~/.feral/settings.json` to
enable anything:

```json
{
  "commerce": {
    "currency": "USD",
    "per_transaction_max": "75",
    "per_day_max": "200",
    "merchant_allowlist": ["noon.com", "amazon.ae"]
  }
}
```

An empty `merchant_allowlist` means any merchant is acceptable; a
non-empty one is exhaustive.

### What is refused, and why

| Situation | Outcome |
|:----------|:--------|
| No caps configured | Refused. Unset is not unlimited. |
| Price could not be read | Refused. An unreadable price is not a small one. |
| Price in another currency | Refused. FERAL never converts: a guessed rate would be the most consequential number in the transaction. |
| Amount over `per_transaction_max` | Refused before you are asked. |
| Today's completed spend plus this amount over `per_day_max` | Refused. |
| Merchant absent from a non-empty allowlist | Refused. |

Only completed purchases count against the daily total, so a day of
browsing cannot lock you out of your own limit. Every evaluation,
including each refusal, is appended to `~/.feral/purchases.db`: "nothing
was bought" and "nothing was attempted" are different facts.

FERAL is never the merchant of record. No tool completes a payment; a
purchase flow stops at `awaiting_confirmation` and the charge, if any,
happens on your own card at the real merchant.

### Approvals reach the device you are carrying

A pending approval is pushed to every node attached to the session, not
just the Mac web UI, as an `approval_request` frame carrying a spoken
sentence, the merchant, and the amount when it is known. It is settled
by an `approval_resolved` frame, so answering on the phone stops the
glasses asking.

Approvals expire after `security.approval_ttl_seconds` (default 300).
Answering a prompt from an hour ago is not consent to run it now.
