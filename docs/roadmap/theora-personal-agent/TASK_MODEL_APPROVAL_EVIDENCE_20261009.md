# Durable model-task approval and processing outcomes

October 9, 2026. This change extends existing TaskFlow, Orchestrator and
ToolRunner authority. It adds no separate action runner. Immutable Mac 9.47 does
not contain this source wave; combined acceptance and exact-source packaging
remain pending.

Natural-language task steps previously treated a returned orchestrator call as
completion even when it only issued an unbound review or produced no final
result. Model calls now retain exact flow, step, call, arguments, resource and
originating surface before dispatch. Reviews bind to that durable identity
before publication. Approval uses the existing central executor once and
continues model processing from recorded results. Changed terms, missing
authority and unknown effects stop progression; they do not repeat effects.

Failure, budget exhaustion, tool-only exhaustion and empty output cannot advance
the flow. Explicit Resume can renew a lost or expired review after fresh policy,
resource and checkpoint checks. It issues a new exact review without invoking
the original goal or dispatching an effect. Existing live reviews remain valid
only under their original authority.

Every goal-bearing step receives bounded persisted goal/constraints and earlier
completed processing results, including after a fresh process. Assistant
processing output is explicitly distinct from verified external action receipts.
Constructed task prompts remain in task/transcript records and bypass both user
Learner extraction and user_command/AboutMe admission. The original committed
user turn retains that ownership. Credential fields are structurally redacted
from result projections and continuation context.

Executed source evidence:

- 289 tests across 15 runtime/task/approval/chat suites passed, seven warnings,
  18.77 seconds. Tests include the actual orchestrator loop with inert provider
  and effect boundaries, immediate approval races, fresh-process continuation,
  exact review renewal, cancellation, uncertain dispatch and origin preservation.
- Independent actual-class postfix probes passed five checks, one warning,
  0.71 seconds. Four verify renewed review, structured credential redaction,
  nested delegation containment and both personal-memory admission paths. One
  characterizes receipt capacity; it does not establish product readiness.
- CI-rule Ruff and whitespace checks passed for the changed files.

Private receipts: feral-task-model-release-receipt-20261009.json,
feral-task-model-provenance-final-integration.log and
feral-oct9-taskflow-review-probes-final.log. Intermediate failed probes and
superseded checks are retained separately. No live model, audio, account,
purchase or browser outcome is claimed.

Remaining contracts and limits:

- Phone-origin background review delivery/resolution and job-scoped Chrome
  delegation still require the explicit bridge in [the iOS handoff](IOS_AGENT_HANDOFF.md).
- Nested subagent and background_task delegation inside durable model steps is
  explicitly refused until causal child ownership exists. Immediate foreground
  delegation remains available. This is a capability gap, not completed
  simultaneous background agent support.
- The independent review also found a legacy explicit skill.invoke unknown-
  surface bypass. The subsequent [creation-owned scope correction](TASKFLOW_ORIGIN_POLICY_EVIDENCE_20261009.md)
  addresses REST, scheduled and historical model/tool dispatch; its own evidence
  and migration boundaries remain separate from the initial tests above.
- The shared model-effect lane still serializes these jobs. Full independent
  concurrent goal jobs and supervised work surviving app Quit remain open.
- A completed processing step is not proof of checkout, delivery or successful
  interaction with a real account. Those require authoritative adapter evidence.
