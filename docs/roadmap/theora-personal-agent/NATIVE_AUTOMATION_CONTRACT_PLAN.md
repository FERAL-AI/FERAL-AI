# Native automation contract audit

Date: 2026-10-02. Read-only audit against HEAD `4e19f07f8` plus the shared,
uncommitted 9.28 preparation. This report makes no source changes and does not
certify the packaged app. No tests or real automation actions were run for this
audit. Statements below are confirmed by the cited source; proposed behavior
and acceptance are future work.

## Verdict

The smallest ready parity card is **legacy scheduled-automation create, list
and delete in Workflows**, with exact review/readback guards. These backend
routes exist today, but this capability is separate from the existing native
Automation destination for geofences and webhooks. Calling the card full CRUD
would be inaccurate: update routes do not exist.

**Routine Run Now requires a backend contract first.** Neither the registered
routine router, routine skill implementation/manifest, scheduler public API nor
the current web Flows page provides it. A native button must not call a private
scheduler function or invent an endpoint. The parity inventory's web routine
"run" wording should be corrected by its owner in a later documentation wave.

## Source-backed inventory

| Surface | Existing contract | Native implementation | Remaining gap |
|---|---|---|---|
| Legacy scheduled automations | `api/routes/timeline.py`: GET/POST `/api/automations`, DELETE `/api/automations/{job_id}` | No controls in `NativeWorkflowFeature.swift` or `NativeAutomationFeature.swift` | Add C/R/D and truthful schedule receipt. No update contract. |
| Routines | `api/routes/routines.py`: create/list/detail/pause/resume/delete/runs | `NativeWorkflowFeature.swift`: reviewed list, enabled creation, pause/resume/delete; `NativeAutomationFeature.swift`: exact detail and bounded last-20-run inspection | Immediate run and edit absent. Existing review/delete guards need strengthening. |
| Geofences | `api/routes/timeline.py`: POST/GET/DELETE `/api/geofences` | Automation already creates, lists and deletes with single-use review and exact readback | Same-name replacement can support bounded edit, but is not atomic conditional update. |
| Inbound webhooks | `api/routes/webhooks.py`: create/list/delete/receive | Automation already creates/lists/deletes; hides secrets | No update/secret-rotation contract. |
| Outgoing webhooks | `api/routes/outgoing_webhooks.py`: create/list/delete/test | Automation already creates/lists/deletes/tests; fixed synthetic delivery and secret masking | No update, enabled toggle or secret-rotation contract. |
| Specialists and evolution | `NativeAgentFeature.swift` | Specialists/evolution reads and documented supported actions | Not the owner of scheduled-automation CRUD. No Agent view edits needed for this card. |

Routers are registered in `api/server.py` through `routines_router`,
`timeline_router`, `webhooks_router` and `outgoing_webhooks_router`. Generic
`POST /api/tools/execute` validates an installed manifest endpoint and cannot
create an undeclared `feral_routines.run_now` capability.

### Legacy automations are scheduled prompt jobs, not general event triggers

`CronService.create_from_natural_language` in `agents/scheduler.py` creates a
recurring CUSTOM job whose payload holds `action_text`, `source` and
`original_text`. The REST route does not supply an LLM parser. Regex parsing
silently defaults unrecognized text to `every 1h`. The web structured builder
can serialize webhook/geofence/event language that this parser does not
understand. Native must not copy the web's claim that all such records execute
on their named event trigger.

GET returns id, description, cron, enabled, next_run and run_count, but not the
full payload or session. Safe detail GET `/api/routines/{id}` can confirm the
CUSTOM job, owner session, original text and action payload. Automation DELETE
returns `success:true` even if `delete_automation` removed no row. Success
therefore requires a valid inventory reread showing the exact id absent, not
only that boolean. GET errors include an error plus an empty list; errors must
remain unavailable rather than a false empty state.

Creation immediately arms a recurring job. There is no parse-preview or
disabled-create endpoint. A native-only card can narrowly support an explicit
schedule builder, such as a bounded `every N minutes` prefix composed with the
exact action text, and expose the existing parser contract in review. Arbitrary
natural-language or event-trigger creation should wait for a backend preview
and exact reviewed terms. Do not duplicate a general scheduling parser in Swift
or silently claim that a parsed result was approved before it existed.

### Existing routine review limits

`NativeWorkflowReview` binds generation/action but does not consume an issued
review id. `perform` revalidates its action against current local rows; it does
not fetch an immutable exact routine snapshot before pause/resume/delete.
Pause/resume verify the returned enabled state using safe routine detail.
Delete currently removes the local row after acknowledgement without proving
backend absence. Session changes invalidate reviews and late replies, but do
not close these same-session stale/repeated-review cases.

`NativeAutomationModel` already has the stronger reusable pattern: issued
single-use review ids, captured originals, fresh inventory comparison, verified
readback, unknown-outcome handling without automatic retry, and connection
generation guards. Reuse that pattern in the Workflows owner instead of adding
another authorization or scheduling system. The routine list GET can restart
the scheduler and remains an explicitly reviewed action. Do not add it to
automatic page refresh or use it as a passive preflight.

### Why immediate execution is a backend card

The private `CronService._fire` calls the existing callback then
`mark_completed`, which changes schedule bookkeeping and may disable a
one-shot. Its boolean means callback returned without raising; it is not proof
that the routine's requested effect succeeded. `api.server.execute_routine_job`
records history and dispatches workflow creation, inline TaskFlows, direct skill
execution or an orchestrator prompt. It returns no typed immediate-run receipt.

These branches are not one uniform full dispatcher: direct skill calls invoke
`skill.execute` after cron-specific preflight; prompted routines record success
after command return unless a budget notice exists; workflow branches record
instantiation rather than whole-flow completion. The source's policy-resolution
exception path continues with no decision. A Run Now route wrapping this
callback and returning `ok:true` would expose these limitations and could
misstate outcomes. It must not use a caller-controlled `auto_confirm` value to
stand in for a one-time exact action approval.

Future Run Now must define preserved schedule/one-shot semantics, session
ownership, request deduplication, schedule-versus-manual concurrency, full
existing ToolRunner/Supervisor policy dispatch, cancellation and restart
uncertain outcomes. Reuse existing TaskFlow/chat-turn receipt contracts and the
same SQLite stores where appropriate; do not create another agent/task/history
stack. Queued, awaiting approval, processing complete and successful effect
remain distinct. A manual run must not quietly resume a paused schedule or
claim a failed/skipped job executed.

### Geofence edit is possible but secondary

`perception/location.py:add_geofence` uses SQLite `INSERT OR REPLACE`, keyed by
name. An edit can preserve the same id/name and send the complete canonical
coordinates, radius, on_enter and on_exit, with reviewed old/new values and
fresh inventory/readback. Renaming would be a different create/delete operation
and should be excluded from the bounded card.

The engine retains its `_inside_fences` state on replacement. Editing a region
does not emit a location event immediately, but a later real location sample
can produce a transition under the new definition. Production action callback
wiring was not found in the audited state/server sources. Stored fence metadata
must not be represented as proven action execution. Fresh-read checks cannot
provide atomic compare-and-swap against another client using current POST.
Inbound/outgoing stores have create/get/list/delete methods, not record-update
methods; deletion/recreation is not a safe substitute for editing because it
changes ids, ingress URLs and delivery/signing relationships.

## Proposed next ownership and implementation

1. **Native scheduled-automation C/R/D plus routine review hardening**:
   exclusively `desktop-native/NativeWorkflowFeature.swift` and
   `desktop-native/tests/NativeWorkflowFeatureTests.swift`. Add a clearly named
   Scheduled automations section; passive `/api/automations` inventory; reviewed
   narrow schedule/action creation and exact persisted detail; exact CUSTOM
   deletion with fresh detail/inventory validation and absence readback. Make
   new and touched routine reviews single-use, generation-bound and exact-term
   checked. Keep pending/unknown outcomes explicit and prohibit automatic
   mutation retries. No `NativeAgentFeature.swift`, APIModel, backend or new
   navigation destination required. If a parser preview or stronger atomic
   contract is required, stop that portion at the backend dependency rather
   than weakening its claim.
2. **Immediate-run backend dependency**: reserve only after design review:
   `feral-core/api/routes/routines.py`, `feral-core/agents/scheduler.py`, narrow
   `feral-core/api/server.py`, `feral-core/skills/impl/feral_routines.py` and
   `feral-core/skills/manifests/feral_routines.json` if exposing it to agents.
   Reuse central dispatcher and existing receipts; propose any ToolRunner/store
   changes separately after exact method design. New focused
   `feral-core/tests/test_routine_run_now_contract.py`, with existing routine
   executor, truthful-outcome, cron-deny and permission suites retained. Then
   wire `.routineRunNow` and typed receipt rendering in the Workflow owner.
3. **Optional geofence edit**: separate exclusive
   `desktop-native/NativeAutomationFeature.swift` and its existing tests, with a
   focused registered-route/real LocationEngine disposable test for actual
   replacement semantics. Do not bundle webhook CRUD updates without their
   missing backend contracts.

No paths listed above are reserved or edited by this audit. Parent must assign
them after packaging the frozen 9.28 candidate. This new evidence file is the
audit's only edit.

## Meaningful acceptance for the first native card

- Extend actual Workflow model fixtures for exact supported schedule, CUSTOM
  type, session id, original/action text, parsed cron and enabled state. Cover
  unknown/HTTP200-error inventory, missing runtime, malformed/duplicate ids and
  repeated/expired/stale reviews without any POST/DELETE.
- Exercise stale payload/type/owner/id and cross-connection changes before
  effects; unchanged readback after lying deletion; create acknowledgement
  without a persisted row; response lost after possible commit; late replies.
  Creation/deletion receipts certify persisted schedule state, never effect
  completion. Keep unrelated schedules and metadata unchanged.
- Test immediate-run unsupported state now: no request to an invented route,
  no private callback and no implicit routine-list scheduler restart.
- Future backend Run Now tests must use real registered ASGI routes, disposable
  scheduler storage and controlled side-effect counters. Check exact approval
  identity, deny/plan/pause/cancel, concurrent scheduled/manual claim, duplicate
  request/reconnect, interrupted outcome without replay, failed/skipped/unknown
  typed outcomes and unchanged future schedule where specified. A callback
  counter alone is insufficient to certify its underlying task.
- After assignment, run `bash test_features.sh Workflow Automation` from
  `desktop-native` and the full native checks required by repository rules.
  Those URLProtocol fixtures are not actual GUI/backend acceptance.
- Packaged acceptance uses a new disposable profile and a safe distant schedule
  with a local read-only action: create, verify exact stored record and displayed
  schedule, relaunch/read, review cancel, delete and prove absence. Keep the
  scheduler controlled so no external tools execute. Real immediate execution
  is a later acceptance gate, after its backend contract exists.

Existing `NativeWorkflowFeatureTests.swift`,
`NativeAutomationFeatureTests.swift`, `test_routine_executor.py`,
`test_routine_outcomes_are_truthful.py`, `test_unfireable_routine_is_disabled.py`,
`test_cron_surface_deny.py`, `test_feral_routines_skill.py` and
`test_automation_time_context.py` were inspected as test sources, not rerun.
