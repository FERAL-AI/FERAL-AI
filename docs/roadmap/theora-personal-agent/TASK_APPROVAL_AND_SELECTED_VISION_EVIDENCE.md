# Exact task approval and selected vision

Updated October 5, 2026. This wave extends the existing tracked turns, central
executor, TaskFlow, provider adapters and native Oversight screen. Current source,
publication and artifact acceptance are recorded in [WORK_STATE](WORK_STATE.md).
They are packaged in [immutable 9.45](NATIVE_9_45_ACCEPTANCE.md); the preceding
9.44 does not contain them.

## Task approval retains its original request

A tracked task proposal carries a private binding to its original owner, session,
request, turn, tool call, surface, reviewed arguments and runtime generation.
After the original turn closes awaiting approval, the approval resolver checks
the durable turn receipt and issues a one-use transfer to the central executor's
actual backing task. A later same-owner approval or conversational “yes” preserves
the proposal's original attribution; the approving turn remains separately
identified. Copying an execution context into another task does not transfer
permission. A recovered runtime generation, changed arguments, cancelled origin,
missing receipt or forged binding refuses execution.

Approval permits creation of that exact TaskFlow. It grants no ongoing tool
permission and does not authorize later task actions. Those actions still use
the existing executor and policy. Creation and inspection receipts remain
distinct from successful external outcomes. Pending approvals are in memory;
this wave does not add restart/reconnect persistence or result subscriptions.

## Native approval scope and availability

The approvals API describes the existing authority with output-only
`approval_scope` metadata: contract version 1 and kind `exact_request` or
`session`. It also reports boolean `approval_available`. Caller-supplied scope
cannot grant permission. Exact browser, workflow and tracked-task requests retain
their own admission checks; ordinary legacy session approval stays compatible.

Native Oversight labels exact requests “Approve this request” and explains that
approval does not mean the task completed. It checks scope and availability again
before posting a decision and requires the returned scope to match its review.
Malformed explicit metadata refuses instead of inferring a session grant.

An authentic tracked request whose original runtime binding is stale can remain
visible with approval disabled and denial available. It cannot block review of
other valid requests. This deny-only projection requires the original private
proof; missing or altered proof is not downgraded to ordinary approval. Expired
requests clear their associated bookkeeping. Historical browser/workflow
availability is not inferred from a tracked-task binding.

## Vision uses the selected provider without changing chat

The existing agentic computer-use skill selects an exact provider, model,
endpoint and matching credential before capture. Selection is passive: it does
not initialize a second model router, probe inference, download weights or switch
the user's chat provider. A different provider requires an explicit model. Known
text-only and unsupported bindings refuse before screen capture.

The initialized shared LLM provider owns the budget and multimodal wire adapter.
A temporary client preserves the selected endpoint and credential even if the
primary provider changes. This decision makes one attempt, retains image input,
and does not fall back to another model. Supplier failures return bounded,
redacted errors. Local calls retain the existing zero-service-cost basis; paid
calls retain budget admission and refusal when image cost cannot be bounded.

An authoritative inventory from the same local endpoint can establish absence.
An inventory from another endpoint cannot. Model classification and presence do
not prove inference accuracy. Selected Ollama context verification still bounds
the actual request, but no longer overwrites the primary chat context observation
or cached context window. Existing capture consent, action gates, bounded loops,
cancellation and unknown-outcome stops remain.

## Verification and remaining acceptance

Parent frozen integration passes 2,724 tests across 124 suites, with two skips
and 67 warnings in 69.24 seconds. All 1,329 Python inputs remain unchanged;
source digest `51a73308c52207842d4acc8085a2b2350037b920f7e905f4abdb678e0de0a14f`.
Full local typing retains 798 existing diagnostics in 232 files with zero
normalized additions or removals, including multiplicities. CI-rule Ruff,
whitespace and documentation navigation pass. The local documentation leakage
checker sees three preexisting ignored private root documents; they are not
tracked or included in publication.

Final worker gates pass 487 core checks across 18 suites, 158 additional
compatibility checks across seven suites, and 575 vision/provider checks across
24 files with six existing skips. Native Oversight passes 27 fixture groups
including three actual API shapes; linked model 28, desktop 39 and error
presentation five checks pass, along with production typecheck. These worker
checks overlap with parent integration.

Worker checks exercise actual imported approval/executor/TaskFlow methods,
SQLite contention and isolated API routes with inert external effects. Native
checks consume real API fixture shapes; provider checks use inert HTTP transport.
Overlapping suite counts are not additive product coverage. Parent frozen
integration, exact-source packaging and packaged-method checks have separate
receipts in the current checkpoint.

The wider compatibility run found an invalidated Chrome approval status
regression and an older expiry fixture missing new approval bookkeeping. Their
failed receipt is retained separately from subsequent corrections and checks.
The preceding remote CI's malformed Ollama inventory fixture is corrected to
require refusal and preservation of prior model IDs; production inventory
validation is not relaxed.

Actual native approval-screen acceptance is blocked by Computer Use's native
pipe startup failure. No real model inference, screen action, microphone,
account, message, payment, glasses, clean-machine installation or signed release
is established by these fixtures. Durable result delivery, trusted desktop
resource ownership, physical voice and the other full-product gates remain in
[request coverage](REQUEST_COVERAGE.md) and [release readiness](RELEASE_READINESS.md).
