# SDK plugin authoring: explicit runtime adapters

Checked October 2, 2026. Worker source is frozen for parent integration. Repository
HEAD during these checks was `634595c7194aedaf6eb9b8c91cbacb87fedea4bd`; the SDK
slice is a working-tree addition and uses the frozen parent runtime wave, including
the DEV01D exact executor admission repair. This is not a publication, native
candidate, clean-machine or unrestricted product-readiness claim.

## Implemented contracts

Python `FeralPlugin.runtime_skill()` lazily creates a zero-argument BaseSkill
adapter class that developers explicitly export in `impl.py`. Standalone SDK
imports/manifest generation require no core import. Missing runtime dependency
raises an explicit error. The plugin itself must have a zero-argument constructor
and the runtime interpreter must also contain `feral-sdk`; no dependency installer
or native payload change was added. Generated endpoints use `PYTHON` with empty
URL, conservative confirm/requires_user_approval metadata and copied defaults.
Async tool definitions, bounded distinct identifiers, parameter shapes and
postponed type hints are checked without inventing an internal transport.

Node `definePlugin` snapshots authored definitions and requires an explicit
loopback HTTP origin when creating a manifest. `startPluginHost` supplies a real
POST service on literal loopback, bearer authentication, credential-free manifest,
JSON body/result bounds, bounded concurrency, redacted failures, handler timeout
and explicit idempotent close. `integer` and defaults are included in manifests.
The former non-routable `plugin://` output is refused instead of certified usable.
The HTTP helper is a Node-only transport, not generic HUP/node authentication.
Existing public client exports and client/thread sources are preserved.

The service token is not FERAL approval: a holder can contact that service directly.
Calls originating through FERAL retain the existing central review and domain
allowlist. No policy relaxation, standing approval, generated-code execution,
account access, dispatcher, API route or core registry repair was added.
`@types/node` 22.20.5 and its type-only `undici-types` 6.21.0 dependency were added
to the scoped Node development lock; no production HTTP client dependency was added.

## Actually verified

From ASOS root with the pinned interpreter:

```sh
.venv/bin/python -m pytest sdk/python/tests -p no:randomly -q --no-cov
.venv/bin/python examples/sdk-authoring/walkthrough.py
.venv/bin/python examples/sdk-authoring/loader_negative_check.py
.venv/bin/ruff check --select E,F,W --ignore E501,E402,F401,W291,W293 sdk/python/feral_sdk/plugin.py sdk/python/feral_sdk/tool.py sdk/python/tests/test_plugin_authoring.py examples/sdk-authoring
```

Full Python SDK suite: **112 passed, 7 warnings in 3.51s**. Authoring adds 20 tests;
existing HTTP, tracked chat and thread fixtures remain unchanged. Warnings are
existing Pydantic/FastAPI deprecations, not a zero-warning claim. Ruff passed.
Direct positive fresh-process walkthrough exited 0 and verified:

- actual package validation and disposable installation;
- actual registered reload route over ASGI, then actual adapter backing lookup;
- real ToolRunner, SkillExecutor, exact review and SQLite approval manager;
- reviewed sum 42 with bound caller session and one handler invocation;
- Deny with zero calls, foreign-session/reused review refusal;
- argument and implementation bounds failure, uninstall, and stale review refusal.

The direct negative probe exited 0 **confirming a remaining defect**: current
reload returns `ok:true` for a bare FeralPlugin module, while the implementation
lookup is absent and no handler executed. This acknowledgement is not readiness.
The SDK adapter positive is verified separately. This negative behavior snapshot
must be updated with the separate future truthful-loader repair; do not preserve
the false ACK as a desired production invariant.

From `sdk/node`:

```sh
npm test
```

TypeScript build plus Node suite: **82 passed, 0 failed/skipped**. New host fixtures
verify real listener/authentication, unauthorized/malformed/oversized request zero
calls, strict UTF8, invalid/nonfinite or oversized result failure, non-loopback and
credential/URL refusal, duplicate definitions, timeout without retry, occupied
capacity until the underlying handler settles, listener release, idempotent close
and duplicate-port refusal. Existing client/thread fixtures are unchanged.

The new runtime integration starts a genuine SDK HTTP host and a fresh pinned
Python process with disposable homes before imports. It installs its generated
HTTP manifest, uses the actual registered reload route, real central ToolRunner
and generic HTTP SkillExecutor, and verifies reviewed sum 42, exact single-use
and foreign review refusal, Deny, domain refusal and uninstall. The actual Node
handler count is **one** across the whole scenario. It uses a private stdin pipe
and in-memory executor key for the ephemeral credential, not commands/logs/OS vault.
The existing domain gate returns a refusal with status_code 0; an initial fixture
expecting 403 was corrected to that source-backed envelope. Distinct blocked,
allowed and removal operations preserve the actual anti-loop guard unchanged.

Socket checks required managed escalation after sandbox `listen EPERM`; dependency
installation required escalation after sandbox DNS failure. npm packing similarly
required access to its cache. No cache ownership repair, global install or account
operation was performed. Earlier failed runs are not passing evidence.

## Package artifact checks

Exact final commands from ASOS root for Python:

```sh
.venv/bin/python -m build --wheel --no-isolation --outdir /private/tmp/feral-sdk-authoring-final-20261002 sdk/python
.venv/bin/python examples/sdk-authoring/check_python_wheel.py /private/tmp/feral-sdk-authoring-final-20261002/feral_sdk-0.1.0-py3-none-any.whl
```

Both exited 0. The extracted wheel is imported in an isolated fresh process,
asserting artifact provenance and deliberately blocking `skills` imports. It
confirms standalone manifest creation plus an explicit missing-runtime error.
Declared dependencies came from the existing pinned interpreter, not a fresh
network installation. Setuptools reported existing license-metadata deprecations.

From `sdk/node`, after the passing final build:

```sh
npm pack --ignore-scripts --pack-destination /private/tmp/feral-sdk-authoring-final-20261002
npm install --prefix /private/tmp/feral-sdk-authoring-final-20261002/node-consumer /private/tmp/feral-sdk-authoring-final-20261002/feral-sdk-0.1.0.tgz --ignore-scripts --offline --no-audit --no-fund
FERAL_SDK_PACKAGE_DIR=/private/tmp/feral-sdk-authoring-final-20261002/node-consumer/node_modules/@feral/sdk node --test tests/plugin_runtime.test.cjs
```

Pack/install exited 0. The final actual-runtime integration used the installed
local tarball rather than source `dist`: **1 passed, 0 failed/skipped in 658ms**.
Only a local artifact was installed in a disposable consumer, with scripts and
network disabled. No SDK package was published.

Artifact SHA256:

- Python wheel: `e9989a986174688cffc1ec7d07240396ec58675dcd0ebab9e0155149ce94905c`
- Node tarball: `a3d62ec8a8c7e6a5bef0970d1d848a65b2a988899bca5e607ecab91dc1f3a1d0`

Production source SHA256 at freeze:

- Python plugin: `f4e0c852c2c45a9186002dc02d233402acb9f1eb5c6b1f4fe7d6f4b726befd67`
- Python tool: `14d0e2dd4cb2558eac9599c111d02b16c948b29e4389e85b536799a768da86e4`
- Node plugin: `fa68c4a998e2e4f5df6937dc86ae29493ce72813d4b0c8642f610d69fcaa5f9d`
- Node host: `a99a4ca81a013dc32f51abb7b7d1c1ace81fcfcb9b3e5dc7c60d9cb2e7fab7cd`
- Node index: `4665263746add3edfa99270cbd17505fb005b69eb01de979e833ae8a9e352165`

## Remaining gates and explicit limitations

Core loader false ACK/import-error handling, failed replacement preservation and
manifest/implementation identity matching remain a separately reviewed card.
This adapter does not repair them. Marketplace/low-level installation alone does
not prove active implementation or invocation. No automatic SDK dependency
installation, boot activation, plugin lifecycle-hook execution or native SDK
bundling was added. Uninstall may leave a cached implementation object, while
registered central dispatch refuses the removed skill.

Trusted local Python/Node handler code is not confined as malicious code. The
Node bearer is service access, separate from central FERAL approval. Handler
semantic validation is still the author's responsibility. Host transport has a
10s inactivity timeout and bounded request/header settings, while handler timeout
is separately configurable; an HTTP disconnect can precede underlying completion.
Closing a listener does not cancel/rollback active handlers. An indefinitely hung
handler can retain its capacity slot until process shutdown; no durable task or
restart recovery is invented. Uncertain output is not safe to retry automatically.

These checks use a controlled embedding and actual registered ASGI handlers, not
a production Brain lifespan, signed-registry journey, clean-clone dependency
install, remote authentication deployment, models, native bundle, Linux/macOS
clean-machine installation or app-store distribution. Those must be accepted
separately. No personal profiles, accounts, purchases, messages, Git changes or
native app assembly/GUI/staging were performed by this worker.

Logs retained in `/private/tmp`:
`feral-sdk-python-authoring-full-20261002.log`,
`feral-sdk-node-authoring-tests-20261002.log`,
`feral-sdk-authoring-python-walkthrough-20261002.log`,
`feral-sdk-authoring-loader-negative-20261002.log`,
`feral-sdk-authoring-ruff-20261002.log`,
`feral-sdk-authoring-wheel-build-20261002.log`,
`feral-sdk-authoring-wheel-import-20261002.log`,
`feral-sdk-authoring-node-pack-20261002.log`,
`feral-sdk-authoring-node-install-20261002.log`,
`feral-sdk-authoring-node-package-runtime-20261002.log`.


## CI placement correction after SDK publication

Parent inspection of SDK commit `205f468a2b783e847acfe810e2e753d3785f89ad`
CI Node job `111076675612` found **81 passed, 1 failed**: the cross-language fixture
tried the repository `.venv/bin/python`, absent from the standalone Node runner,
and spawn returned ENOENT. The earlier local82 result was valid local evidence,
not a passing CI claim. No runtime test is removed or marked skipped.

The narrow correction preserves `npm test` as all mandatory checks, adding:

- `test:unit`: build plus standalone client/thread/host tests, 81 tests;
- `test:runtime`: build plus actual central-runtime invocation fixture, one test;
- explicit `FERAL_TEST_PYTHON` interpreter selection, defaulting to the repository
  pinned environment only for local execution. Explicit configuration must be
  absolute, a regular file, readable and executable, with no control characters.
  Invalid/missing paths fail before host startup or credential handoff.

Parent owns the CI workflow change: standalone Node22 runs `test:unit`; both
`brain-tests` and `brain-tests-pr` run mandatory `test:runtime` after core dependency
installation, Node22 setup and `npm ci`, passing the absolute active interpreter
with `FERAL_TEST_PYTHON="$(command -v python)"`. The worker read/parsed that actual
workflow and verified both runtime steps are mandatory, not continue-on-error,
and use working-directory `sdk/node`. It did not edit the workflow or publish Git.
New remote CI success is not claimed before its result arrives.

Local checks used Node25.4.0; the wired remote Node22 rerun remains pending.
Final local commands from `sdk/node`, all exited 0:

```sh
npm run test:unit
npm test
FERAL_TEST_PYTHON=/Users/mahmoudomar/Desktop/thoera-mac/ASOS/.venv/bin/python npm run test:runtime
```

Results: unit **81 passed, zero skipped**; all **81 unit + 1 actual runtime passed,
zero skipped**; explicit interpreter **1 actual runtime passed, zero skipped**.
The all command verified the default pinned interpreter on the final fixture.
There is no changed dependency/lock, public client or production runtime source.
`git diff --check` passed for the owned SDK paths.

Seven isolated negative Node processes exercised the actual fixture with missing,
relative, empty, directory, nonexecutable, unreadable and control-containing
interpreter paths. Each exited nonzero with a clear interpreter diagnostic;
a preload sentinel verified the host-start function was **never called**.
These are intentional failure probes, not seven passing runtime integrations.
The reproducible probe script is retained at
`/private/tmp/feral-sdk-ci-negative-probe.py`; its result is
`/private/tmp/feral-sdk-node-ci-repair-negative-20261002.log`.

Additional final logs:
`feral-sdk-node-ci-repair-unit-20261002.log`,
`feral-sdk-node-ci-repair-all-20261002.log`,
`feral-sdk-node-ci-repair-runtime-explicit-20261002.log`.
The prior package artifact hashes above refer to the artifact check before this
CI-script metadata change; no newly packed/published SDK is asserted by this fix.

## Desktop SDK dependency integration

Parent added the local Python SDK to the existing noneditable staged-interpreter
install, using the same `feral-core/requirements.lock` constraints. This fixes
the earlier absence of `feral_sdk` from the desktop runtime; it does not add a
general extension dependency installer or repair reload acknowledgement.
`bash -n desktop/scripts/stage_bundle.sh` passed. Actual staging exited0:
CPython3.11.15/SQLite3.53.1/FTS5, v2 web UI import, SDK import physically inside
the staged interpreter and an SDK-created adapter binding actual `BaseSkill`
all passed. The probe constructs a harmless adapter and executes no plugin tool,
model, account or external effect. Log:`/private/tmp/feral-native-9-30-stage.log`.

Next9.30 assembly must independently verify the same SDK dependency in its final
payload. These staging results are not yet packaged GUI or clean-machine
acceptance; immutable9.29 remains unchanged and contains no SDK follow-up.
