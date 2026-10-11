# Registry reload preparation and recovery

October 2, 2026. Implementation and controlled local verification; not a release
or clean-machine installation certification. The earlier
[design](REGISTRY_RELOAD_RECOVERY_PLAN.md) remains a historical proposal.

## Implemented behavior

Installed/generated package reload prepares the exact selected manifest, tools
and Python callable before publishing any live replacement. A Python package must
export one unique `BaseSkill` class, with matching requested/manifest/instance ID
and a concrete asynchronous `execute`. Aliases of the same class are accepted,
including the standalone SDK's explicit runtime factory export. Missing,
ambiguous, mismatched, inherited-base and failing implementations return failure.
An existing higher-priority broken package does not fall through to a lower source.

Manifest and implementation reads are bounded at 1MiB and 2MiB respectively.
Identifiers are bounded and reject path separators/control characters. The
configured home is canonicalized once, preserving legitimate macOS `/var` aliases;
redirected package directories and source files beneath it are refused.

Helper-mediated decorator/instance registrations are captured during candidate
import and construction. They cannot replace live backing before publication or
register another skill. A closed inherited capture refuses delayed registration.
No global dictionary snapshot is restored, so unrelated concurrent writes survive.
Captured implementation bytes are compiled under a private module name, avoiding
timestamp-based stale pyc loads; the stable old module remains until publication.
Owned failed staging modules are removed. Replacement retains only the current
private module alias plus its stable name; repeated publication is refused without
removing the live alias.

Publication checks generation and the previous manifest, tools, backing and stable
module identities, as well as manifest terms and selected source bytes. A changed
baseline/source is refused. Successful publication changes manifest/tools/backing
together in a no-await section, increments generation once, and then creates
existing manifest routines. Failed preparation creates no routine and retains the
working implementation. Explicit HTTP/WS transport replacement can omit Python;
successful replacement removes old Python interception.

The real async route prepares imports/constructors off the event loop. Cancellation
before publication discards a candidate that completes later. Preparation tasks
remain strongly retained until completion, and late failures log only exception
class diagnostics after retrieving the exception. Immediately before publication
on the owning loop, a trusted internal guard requires the captured registry to
still be the live state registry. Replacement of that owner returns conflict and
publishes to neither the old nor new registry. Legacy route exceptions return a
bounded `reload_raised` error and log their class without private exception text.
Bounded source
comparison still performs synchronous file reads immediately before publication.
Startup installed-package loading and lazy backing loading use the same contract.
A failed lazy load is not automatically retried on later lookup; explicit reload
can retry after a package repair.

Shipped manifest-only reload preserves the existing inventory refresh behavior and
retains matching already-wired backing without constructing account integrations.
The successful route adds `refresh_kind: manifest_inventory|package` and
`implementation_ready`. The latter describes validated Python backing, not account
configuration, transport reachability or complete endpoint correctness. A shipped
inventory refresh can succeed with `implementation_ready:false`; it does not
certify that integration as available. Legacy `ok/skill_id` and failure
`ok/code/error` response fields are preserved.

## Actual verification

Run from the ASOS checkout with the pinned interpreter and disposable homes.
No private account, external service, model, native GUI or staged app was used.
Existing test assertions were preserved except the authorized SDK bare-plugin
negative case, which now requires actual refusal rather than the previous false
ACK, and the legacy raised-error assertion, which now requires redaction while
preserving the HTTP 500 and failure assertions.

```sh
cd feral-core
../.venv/bin/python -m pytest tests/test_registry_reload_recovery.py \
  tests/test_skill_registry.py tests/test_skill_hot_reload.py \
  tests/test_skill_approval_reporting.py tests/test_install_reports_hot_reload.py \
  tests/test_marketplace_install_consent.py \
  tests/test_tool_runner_validator_invalidation.py \
  tests/test_skill_validator_matches_calls_not_words.py \
  -p no:randomly -q --no-cov
../.venv/bin/mypy skills/registry.py skills/impl/__init__.py \
  api/routes/skills.py --follow-imports=silent --check-untyped-defs
```

- Focused runtime suite: **159 passed**, seven existing deprecation/environment
  warnings, 3.23 seconds. New recovery suite contributes 41 cases. Log:
  `/private/tmp/feral-registry-reload-focused.log`.
- Targeted mypy: **no issues in three source files**. This is not a full-repository
  typecheck or remote CI ratchet. Log: `/private/tmp/feral-registry-reload-mypy.log`.

```sh
.venv/bin/python -m pytest sdk/python/tests -p no:randomly -q
.venv/bin/python examples/sdk-authoring/walkthrough.py
.venv/bin/python examples/sdk-authoring/loader_negative_check.py
.venv/bin/python examples/developer-runtime/walkthrough.py
cd sdk/node
npm run test:unit
npm run test:runtime
```

- Python SDK: **112 passed**, seven existing warnings, 3.58 seconds. Log:
  `/private/tmp/feral-registry-reload-sdk-python-full.log`.
- Actual Node loopback HTTP-host/registry/ToolRunner/SkillExecutor fixture:
  **one passed, zero skipped**. The sandbox initially refused loopback listening
  with `EPERM`; the approved rerun passed. Log:
  `/private/tmp/feral-registry-reload-node-runtime.log`.
- Node client/plugin unit compatibility: **81 passed, zero skipped**. Together
  with the mandatory runtime case, all 82 Node SDK cases passed. Log:
  `/private/tmp/feral-registry-reload-node-unit.log`.
- Both real local developer walkthroughs returned reviewed sum42 exactly once,
  zero execution on Deny, foreign/reused review refusal and stale-uninstalled
  refusal. The bare-plugin probe returned `reload_acknowledged:false`, no backing
  and no execution. Logs: `/private/tmp/feral-registry-reload-walkthrough.log`,
  `/private/tmp/feral-registry-reload-negative.log`, and
  `/private/tmp/feral-registry-reload-developer-walkthrough.log`.
- New tests use actual registered ASGI routes and controlled import events to
  verify responsive reads while construction waits, cancellation/late cleanup and
  unrelated registration preservation. Real ToolRunner and SkillExecutor verify
  V1 invocation after failed V2 preparation, fresh reviewed V2 invocation after
  successful publication, exact one-use review, foreign-session refusal and Deny.
  Independent task sessions preserve the existing anti-loop guard.
- Barrier-controlled real route tests replace the live registry during candidate
  construction and prove conflict without publication to either registry.
  Cancellation tests verify strong task retention until worker completion, late
  error retrieval and class-only diagnostics. A legacy exception sentinel is
  absent from both the actual response and captured logs.
- Ruff using the repository's CI rules and whitespace checks passed. No `Any`,
  cast or ignore was introduced to resolve new type errors.

## Historical policy finding and separate repair

A separate actual disposable probe declares `policy_fixture__read` with
`requires_user_approval:true` and `read_only_hint:true`. The resolver returns
`confirm`, but strict ToolRunner uses the non-strict read-name heuristic and
executes the known read-only fixture without pending review. Observed:

```json
{"autonomy":"strict","handler_calls":1,"policy_decision":"confirm","returned_pending_approval":false}
```

Before the separate policy repair, the assertion requiring pending approval and
zero calls failed. Reproducer:
`.venv/bin/python /private/tmp/feral-registry-policy-override-probe.py`; log:
`/private/tmp/feral-registry-policy-override-probe.log`. This historical result was
outside the registry card. The separate
[manifest approval repair](MANIFEST_APPROVAL_PRECEDENCE_EVIDENCE.md) preserves
resolved confirmation policy in strict ToolRunner without changing the resolver.
Its324 passing regressions include this read-name/hint conflict. The registry
calculator fixture alone does not certify that policy path; the earlier failing
probe remains preserved rather than relabeled as a passing result.

## Boundaries

This stages live-map publication, not arbitrary Python import effects or disk
transactions. Trusted imports/constructors can have effects or block indefinitely;
cancellation does not kill a Python worker thread or roll those effects back.
Direct dictionary mutations and unrelated side effects from malicious trusted code
are not confined. Reload does not add a sandbox or a new security-scanner policy.

Compare-and-publication is bounded observed-drift protection, not filesystem CAS or
linearizability for arbitrary thread readers/in-flight executions. Existing review
records do not bind a code hash. Retired staging-module aliases are pruned, so
long-lived pickled/import-by-name references to retired implementation classes are
not a supported hot-reload recovery contract. Plugin lifecycle hooks remain outside
this card. Clean-clone process installation, full integrated CI and actual native
acceptance remain separate gates; immutable 9.30 resources were not changed.

## Browser bridge type compatibility follow-up

The live `BrainState._bind_browser_bridge` wrapper now inherits `BaseSkill` and
initializes its ID through `super`. The production diff is limited to that local
import, class base and initializer. Dispatch/error envelopes and shared controller
injection are unchanged. No cast, baseline adjustment or annotation suppression
was used.

Six controlled tests call the actual BrainState binding/registration and async
dispatch methods, using the production class without its unrelated subsystem
constructor. They verify success/error envelopes, disconnected-controller failure,
WebActions controller injection, and actual registered-route shipped reload. Reload
retains the exact backing/controller instance and unchanged shipped safety/schema
terms. `manifest_inventory`/Python-wrapper readiness performs no browser/account
verification; disconnected dispatch can still fail normally. An unbound shipped
inventory refresh reports `implementation_ready:false`. Fixtures start no browser,
account, model or application lifespan. State imports occur after disposable
test-home setup, not during test collection.
The initial fixture version imported `api.state` during collection and failed
before tests with SQLite's read-only write refusal. That failed collection is
historical evidence. Deferring the import until the existing disposable-home
fixture is active fixed the test setup; the import/storage safety guard was not
disabled or bypassed.

```sh
cd feral-core
../.venv/bin/python -m pytest tests/test_browser_bridge_reload.py \
  tests/test_registry_reload_recovery.py tests/test_skill_hot_reload.py \
  tests/test_browser_use_endpoints.py tests/test_brain_browser_alias_map.py \
  -p no:randomly -q --no-cov
../.venv/bin/mypy api/state.py skills/registry.py skills/impl/__init__.py \
  api/routes/skills.py --follow-imports=silent --check-untyped-defs
```

The focused browser/registry check passed **453 tests**, seven existing warnings,
2.33 seconds; log `/private/tmp/feral-browser-bridge-reload-tests.log`.
Ruff with CI rules and whitespace checks passed.

The stronger four-file mypy invocation remains nonzero with **31 existing
diagnostics** in `api/state.py`; it is not a clean typecheck. A matching temporary
shadow-file pre-bridge run produced 32. Comparing diagnostic messages independent
of shifted line numbers removed exactly the `_BrowserSkillBridge` argument-type
error and added none. Logs `/private/tmp/feral-browser-bridge-reload-mypy-before.log`
and `/private/tmp/feral-browser-bridge-reload-mypy.log`. The previously clean
three-file check and earlier broader 643-test/819-error snapshots predate this
bridge follow-up. Final full integrated tests and resolved global mypy comparison
remain integrator-owned gates.
