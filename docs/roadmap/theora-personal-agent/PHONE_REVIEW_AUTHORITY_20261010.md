# Private phone task reviews

The existing ToolRunner and central approval dispatcher now bind exact task
reviews to their authenticated originating device. Private ownership is captured
from a verified tracked background-task start or the creation-owned TaskFlow
column and exact action checkpoint. Client fields, node aliases and membership
in a shared conversation cannot supply that ownership.

The private binding stays outside public pending rows, model context and wire
receipts. The versioned public task_review card identifies the request, originating
conversation and exact terms digest; model-action cards also identify the flow,
step and action. Its digest covers the issued request, tool, arguments, resource,
surface and expiry. The card is correlation data, not an action grant.

## Transport and decisions

Private approval_request and approval_resolved frames are queued only to current
authenticated connections of the originating device. Captured socket, credential,
runtime and intake identity are fenced. Missing private delivery never falls back
to a shared-session broadcast. Queue acceptance does not mean delivered or seen.
The local operator inbox and ordinary operator review behavior are retained.

Paired clients explicitly request their inbox with:

```text
GET /api/approvals?session_id=<originating-session>&task_review_version=1
```

Approve or reject through the existing request-specific REST endpoints, with
exactly this body, preserving the complete server-issued card and value types:

```json
{
  "session_id": "origin-chat",
  "task_review": {
    "version": 1,
    "request_id": "<server-request-id>",
    "origin_session_id": "origin-chat",
    "kind": "taskflow_action",
    "terms_digest": "<server-issued-digest>",
    "flow_id": "<server-flow-id>",
    "step_id": 1,
    "action_id": "<server-action-id>"
  }
}
```

Use the complete received object rather than reconstructing these placeholders.
Both owner authentication and the exact card are required. The server privately translates
the originating conversation to existing execution identity, then uses the same
policy, resource, context, cancellation and exact-once dispatch path. Foreign,
changed, expired, stale or unowned decisions cannot execute. Credential loss
after an effect may leave an unknown response; recorded receipts are preserved
and retry_safe is false. Do not replay the action to recover its response.

## Explicit restart recovery

A persisted waiting model-action review appears as renewal_required with
approval_available false after its volatile pending grant is gone. A freshly
authenticated credential for the same still-paired device can request:

```text
POST /api/approvals/<old-request-id>/renew
```

Use the same exact body/card convention. Renewal validates private flow ownership
and the stored waiting model-action checkpoint, reevaluates current policy and
resource identity, and issues a new request/card before publication. Renewal
dispatches nothing; a separate exact approval is required. Old grants are not
restored. Changed, running, cancelled or uncertain checkpoints refuse renewal.
Credential loss during the database commit rolls it back.

Initial pre-TaskFlow proposals remain live-owner-bound and volatile; restart
does not reconstruct them. Legacy NULL-owner records, ordinary untracked reviews,
free-text/node acknowledgments and generated skill_approval do not gain phone
authority. This is review isolation within the existing single-user deployment,
not general multi-user account isolation. Chrome delegation remains a separate
resource contract; an execution-owner mismatch refuses phone projection rather
than rewriting browser ownership.

The separate iOS client must retain exact cards, request version 1, show renewal
as a separate action and preserve unknown-outcome responses. No iOS source or
physical phone acceptance is performed by this backend change. Source checks,
immutable package identity and remaining gates are recorded in
[WORK_STATE](WORK_STATE.md).
