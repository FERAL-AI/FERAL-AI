# Proposed truthful registry reload and replacement recovery

Read-only design checkpoint, October 2, 2026. **Not implemented.** Parent published
core `90b75587a` and SDK `205f468a2b783e847acfe810e2e753d3785f89ad` before this audit.
Production and test sources were not edited for this card. CI placement repair
for the SDK runtime fixture now takes priority; return to this plan afterward.

## Observed source contracts and fresh checks

`skills/registry.py` registers marketplace manifests before attempting dynamic
imports. Both import helpers swallow import/constructor failures. Reload removes
old manifest/cache entries, registers a replacement and increments generation
before importing it. `_reimport_dynamic_impl` also removes/replaces the stable
`sys.modules` entry before success. A failed implementation can therefore leave
new schemas with old backing logic and still return success. `get_skill` may
later lazily repeat imports. A failed higher-priority installed package can fall
through to generated/shipped candidates, concealing the selected source's failure.

`api/routes/skills.py` already maps registry false results to HTTP 409 with
`ok:false/code/error`, and unexpected exceptions to 500. Its current async reload
handler invokes synchronous disk/import/constructor work on the event loop.
`skills/impl/__init__.py` exposes global implementation registration; decorators
and register_instance can publish during import independently of the registry.
Staging only the registry's own final call is insufficient for these helpers.

`SkillPackage.load` validates manifest shape, not code safety or implementation
readiness. SkillValidator's AST scanner is a conservative install preflight, not
a sandbox; marketplace distinguishes SECURITY findings from warnings. Reload
currently does not apply that validator. Do not silently make all warnings hard
failures or describe trusted in-process plugin code as confined. Invocation remains
under the existing ToolRunner/SkillExecutor policy, session and exact-review gates.

Fresh baseline commands from ASOS root, with disposable homes before imports:

```sh
.venv/bin/python examples/sdk-authoring/walkthrough.py
.venv/bin/python examples/sdk-authoring/loader_negative_check.py
```

Both exited 0 in this audit. Positive: actual registered reload route, discovered
SDK BaseSkill adapter and actual central executor produced reviewed sum42 once;
Deny had zero calls, foreign/reused reviews and removed registration were refused.
Negative: bare FeralPlugin module got reload ACK but no implementation. The false
ACK is a defect to remove, **not a desired invariant to preserve**. Logs:
`/private/tmp/feral-registry-reload-baseline-positive-20261002.log` and
`/private/tmp/feral-registry-reload-baseline-negative-20261002.log`.
No new tests, accounts, models or native app were exercised.

## Smallest coherent proposed implementation

1. Select one authoritative source in existing priority order. Validate a bounded
   caller ID before constructing paths; reject separators/traversal/control
   characters without silently trimming or broadening existing legitimate IDs.
   Bind resolved directory to the selected source and reject escaping symlinks.
   Require requested ID = manifest ID = constructed implementation.skill_id.
   The shipped fast-path filename must also declare the requested ID. Once an
   existing selected source fails, report that failure; do not claim another source
   repaired it. Preserve the eight shipped filename/declared-ID differences.
2. Prepare a candidate without publishing live state: parse exact manifest input,
   produce projected tools, import known trusted code and construct backing
   instance. Require a real implementation for PYTHON endpoints. Pure HTTP/WS
   manifests remain valid without Python backing. If impl.py exists but fails,
   fail rather than falling back to HTTP/old backing. Distinguish missing,
   invalid, ambiguous and failed implementation with typed bounded reasons.
   Deduplicate aliases of the same exported class. Require a unique explicit
   BaseSkill candidate and implemented async execute; do not select the first
   arbitrary imported class. Do not filter by class.__module__: the SDK factory
   creates its adapter class in feral_sdk.plugin, then explicitly exports it.
3. Stage helper-mediated `register_skill`/`register_instance` writes in an existing
   implementation-registry capture scope instead of mutating the live map during
   preparation. Route decorator registration through that helper. Reject foreign
   IDs/ambiguous registrations. Never restore an entire global snapshot, which
   could erase unrelated concurrent writes. Keep old stable module entry until
   successful publication and clean only owned staging entries on failure.
   Direct malicious mutations of global dictionaries or arbitrary import/constructor
   effects remain outside this trusted-code contract; no sandbox/rollback claim.
4. Prepare route work off the event loop, then compare-and-publish in a short
   no-await section on the owning event loop. Capture old manifest/backing/module
   identities and registry generation; refuse a changed live baseline before
   publication. Parse/compile captured source to avoid timestamp-based stale pyc
   loads. Check selected disk terms again to refuse observed source drift. Keep
   complete schema/manifest/backing old until all candidate checks pass; increment
   generation once, then create routines only for the successfully published
   manifest. Cancellation before publication must leave old state untouched even
   if a worker-thread constructor finishes later. Keep synchronous reload APIs
   compatible by using the same prepare/publish helpers. Do not blindly offload
   the entire current mutating reload function.
5. Share this preparation contract with startup marketplace loading and lazy
   loading so they cannot restore the false-success path. Pure shipped manifest
   refresh can retain an already-wired matching backing instance without
   reinitializing account integrations. When a local Python backing is intentionally
   removed for an HTTP replacement, remove only the matching old backing at
   successful publication; never leave it intercepting the new HTTP manifest.
   Failed preparation retains old manifest/cache/backing/module and creates no
   new routine. No new approval store, dispatcher or plugin lifecycle activation.

This is staged **live-map publication**, not a SQLite/disk transaction or rollback
of plugin import effects. Synchronous cross-thread readers and in-flight tool
calls may have already captured old schemas or instances. A broader immutable
execution snapshot/version-bound review contract would require ToolRunner/executor
coordination and is a separate proposed card. Do not claim linearizable hot
replacement for arbitrary threads or that pending approvals bind a code hash.
The exact staging-module naming/retention must preserve class identity and avoid
unbounded aliases; dataclass/import references need tests before settling that
implementation. Do not temporarily replace global sys.modules stable entries
while user imports run. No safe timeout for arbitrary blocking Python constructors
is invented by a thread offload.

## Proposed exclusive paths and dependencies

Subject to a separate parent grant after frozen CI/full-run work:

- `feral-core/skills/registry.py`: source selection, candidate preparation,
  compare/publication, startup/lazy consistency and truthful detail result.
- Narrow `feral-core/skills/impl/__init__.py`: helper registration capture and
  conditional publication, preserving builtin/autoload contracts.
- Narrow `feral-core/api/routes/skills.py`: asynchronous staging offload and
  cancelled/conflicting publication response, preserving existing response shapes.
- Existing `test_skill_registry.py`, `test_skill_hot_reload.py`; new
  `test_skill_reload_staging.py` for actual backing/replacement/cancellation.
- SDK negative probe and its exact expected-result test must be updated as part
  of integration, with SDK worker coordination; no SDK changes are authorized
  by this read-only card. Existing positive SDK and developer walkthroughs must
  continue using the real executor.
- New implementation evidence file, rather than relabelling this plan implemented.

No API/state/orchestrator/ToolRunner/executor/marketplace/package/native mutation
is proposed for the minimum card. Applying a new mandatory security scanner at
reload, changing install consent/update policy, isolated imports, durable plugin
operations and active-call version binding require separate reviewed scopes.

## Meaningful acceptance matrix

Disposable homes, known read-only authored handlers and pinned interpreter:

- Positive SDK factory export: real package install, actual route, backing and
  authorized invocation; exactly one handler execution. HTTP/WS no-impl and all
  existing shipped declared-ID reload tests remain valid.
- Bare/no class, missing dependency, syntax/import/constructor failure, base
  execute inherited, wrong instance ID, wrong manifest ID/path, ambiguous classes,
  foreign registration, duplicate alias, module-cache freshness and selected-source
  failure with a valid lower-priority source. Failure reports non-success.
- Existing working V1 actual invocation, broken V2 reload, then actual V1
  invocation still works under fresh exact review. Same manifest/cache/backing/module
  object and generation preserved; no routine creation on failure. Valid V2 swaps
  once, later calls use V2; unrelated registration remains untouched.
- Helper registration followed by exception cannot replace live backing. No
  whole-map rollback. Read-only constructor staging uses controlled events; route
  loop stays responsive, cancellation before publication does not activate V2,
  concurrent registration/source edits produce explicit conflict.
- Intentional PYTHON-to-HTTP replacement cannot retain old backing interception;
  failed replacement retains V1. Startup/lazy loading cannot register a unusable
  PYTHON-only package or report it active. Deny/plan/session/exact-review protections
  stay in existing execution path.
- Registry/helper/route suites, installation-consent/hot-reload reporting suites,
  SDK full suites and both actual developer/SDK walkthroughs. Run required broader
  backend checks only after parent freezes the integrated source.

Do not turn a bare-plugin failure into a skipped test, claim readiness from ACK,
call imports free of effects or assert a clean-clone/native installation without
performing those separate checks.
