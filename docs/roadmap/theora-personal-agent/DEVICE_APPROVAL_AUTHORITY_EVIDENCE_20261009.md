# Device approval authority boundary

October 9, 2026. Paired-device authentication establishes a device identity;
it does not establish ownership of every pending tool review. The current store
has no durable authenticated originating-device principal for these requests.
Client session IDs, node aliases and broadcast session membership cannot replace
that missing authority.

## Implemented boundary

Paired REST callers receive typed 403 approval_device_authority_unavailable before
pending reviews are listed, approved or rejected. The profile operator inbox
retains its existing exact session, action, cancellation and one-time rules.

The authenticated /v1/node receive loop binds a private task-local node ingress
restriction. Spawned chat, text and audio work inherit it. Central Orchestrator
tool-review resolution refuses approval and rejection before pending lookup;
plain yes/no acknowledgements are consumed with an operator-inbox instruction,
without model fallback or new effects. Resetting the receiver does not clear the
restriction in its already spawned children. Shared node keys are device ingress,
not profile operator approval authority. No tool executor is replaced.

This is an explicit unavailable capability until trusted request ownership is
added. It does not implement origin-facing background review projection. Existing
session broadcasts may still send review metadata to paired nodes; the new
boundary does not claim fully origin-authorized publication or private delivery.
Generated-skill proposal authorization is a separate tracked contract.

## Executed evidence

Nine isolated suites pass 207 tests. New actual-boundary regressions use the real
HTTP authentication middleware, registered HUP routes, Orchestrator, TaskFlow and
ToolRunner with inert model/effect collaborators. They verify paired inspection
and resolution refusal, no private inbox read, preserved pending reviews, spoofed
context refusal, affirmative/negative replies over chat and text-command channels,
all node credential forms, inherited child restrictions, and unchanged valid
operator resolution. No real message, purchase, account or model operation occurs.

Independent counterchecks pass the actual paired REST boundary and four HUP
acknowledgement/reply-mode combinations with zero effects and no model fallback.
The frozen combined gate passes 4,766 tests, 22 skips, 571 warnings across 234 selected
suites in 159.66 seconds with no source drift. It is not the full repository suite.
Configured pinned Mac mypy remains 796 existing diagnostics with zero normalized
additions/removals and unchanged source hashes; the Linux baseline is untouched.
The final test fixture import is reconciled separately for lint, then its 14
node authority cases are rerun. Production sources remain unchanged.

Private receipts: feral-phone-approval-auth-release-20261009.json,
feral-oct9-hup-approval-origin-fixed-probe.log,
feral-oct9-phone-approval-origin-fixed-probe.log,
feral-oct9-source-wave-d9q87ny4/receipt.json and
feral-oct9-mypy-approval-compare/comparison.json. Earlier defect characterizations
and lint failures remain separate; they are not accepted product behavior.

## Next ownership handoff

Extend the server-owned ingress identity with the verified paired principal and
current peer. Negotiate additive tracked phone turns through the existing
ChatTurnManager using stable request IDs, while retaining reply correlation.
Persist private provenance alongside receipts and TaskFlow creation; bind every
review before publication. Only then permit a matching authenticated origin to
inspect/resolve its review and translate internally to the existing execution
session. Legacy records remain unavailable; do not backfill ownership from client
claims or automatically restore grants. Reconnect, cancellation, changed terms,
revocation, duplicate replies and unknown external outcomes need actual-route
regressions before lifting the restriction. Task-scoped Chrome delegation follows
as a separate resource contract.
