# SDK tool authoring and verified local invocation

These examples extend the existing FERAL runtime. They exercise known read-only
integer arithmetic, not generated code, account access or arbitrary scripts.
The separate `examples/developer-runtime` example uses BaseSkill directly;
this directory verifies the public Python and Node authoring helpers.

## Prerequisites and evidence boundaries

Use the repository's pinned `make dev` environment, Python 3.11 or later, and
Node 22 or later. Install the Python SDK in the runtime interpreter for actual
package use. The repository walkthrough temporarily adds source paths, which
is explicit development setup, not automatic dependency installation.
The native bundle does not yet prove that the Python SDK is available inside it.
Node prerequisites are `npm ci` and `npm run build` in `sdk/node`.

Every runtime check sets a disposable `FERAL_HOME` and `FERAL_DATA_HOME` **before
runtime imports**. It uses existing package installation, registry, the actual
registered reload route over ASGI, real ToolRunner, SkillExecutor and SQLite
review store. A small embedding supplies those existing objects; it does not
start the Brain lifespan, models, scheduler or OS vault. Package installation
refuses pre-existing destinations in these checks because the low-level installer
itself permits replacement. No personal runtime should be used for verification.

## Python

`python_calculator/impl.py` defines FeralPlugin with an explicit exported
`SDKCalculatorSkill = SDKCalculator.runtime_skill()`. The factory is lazy:
standalone SDK imports/manifest generation do not require feral-core, while
runtime class creation requires `skills.base` in that interpreter.
The walkthrough generates the manifest from that exact plugin and installs both
files in a temporary skill directory.

From the repository root:

```sh
.venv/bin/python examples/sdk-authoring/walkthrough.py
.venv/bin/python examples/sdk-authoring/loader_negative_check.py
.venv/bin/python -m pytest sdk/python/tests -p no:randomly -q --no-cov
```

The positive result verifies sum 42 exactly once after a session/action-bound
single-use review, Deny with zero handler invocations, foreign/reused review
refusal, bad/bounded arguments, uninstall and refusal of an earlier review after
uninstall. A loader ACK alone is not considered sufficient: the actual backing
class and invocation are checked. The negative check requires a bare FeralPlugin
class to be refused without registering a manifest or backing implementation.
Installed Python replacements are prepared and validated before publication;
failed replacements retain the working implementation. This is not a sandbox
or rollback guarantee for arbitrary trusted Python import effects.
The loader currently does not call plugin load/unload lifecycle hooks, and
uninstall does not remove every cached implementation object; registration gates
subsequent central dispatch. Neither implementation is a malicious-code sandbox.

For your package, author `impl.py` as above and write
`YourPlugin().to_manifest()` to `manifest.json` alongside it. Use the runtime's
existing package installation/reload contracts and FERAL review UI. The generated
PYTHON endpoint URL is empty and the generated safety metadata requires review.
Do not claim a direct `plugin.execute()` call had FERAL authorization.

## Node

`node_host.cjs` is an executable example in a Node project where `@feral/sdk` is
installed. Supply a private `FERAL_PLUGIN_TOKEN` (32 through 512 printable ASCII
characters) in the environment and a **new** output manifest file path:

```sh
node node_host.cjs /path/in/your/project/manifest.json
```

The process starts a loopback HTTP host and writes its credential-free manifest;
it neither installs it nor grants FERAL permission. Keep the process alive.
Configure the host's bearer credential for `sdk_node_math` privately using
FERAL's existing skill credential vault (or the explicit development runtime
`FERAL_KEY_SDK_NODE_MATH` environment convention). Respect the deployment's
domain allowlist; do not silently relax it. Restarting the host on a new ephemeral
port requires an explicit manifest update/reload and matching current credential.
The sample refuses to overwrite an existing manifest file.

To verify the real host and existing runtime instead of using a personal Brain:

```sh
cd sdk/node
npm ci
npm test
```

The new `plugin_runtime.test.cjs` starts the real SDK HTTP host, then starts the
pinned Python interpreter with `node_runtime_check.py`. That process installs and
reloads the HTTP manifest and invokes the **actual** generic HTTP SkillExecutor
through ToolRunner: reviewed sum 42, foreign/reused review refusal, Deny, domain
refusal and uninstall are verified. The service sees exactly one invocation.
The ephemeral token goes over a private stdin pipe and in-memory executor key
store; it is absent from commands, normal output and manifests. No mocked HTTP
success or model runtime is involved. Existing domain-refusal envelopes use
`success:false, status_code:0`, distinct from host authentication's HTTP 401.

The service credential can also be used directly by a holder outside FERAL;
that is service authentication, not central FERAL consent. A hung handler is not
cancelled by HTTP timeout or listener shutdown. Capacity remains occupied until
it settles, failures after dispatch are outcome unknown, and no automatic retry
is made. Handlers must validate their own semantics and avoid blocking work.

## Package artifact checks

From the repository root, after building the Node package:

```sh
.venv/bin/python -m build --wheel --no-isolation --outdir /tmp/feral-sdk-artifacts sdk/python
.venv/bin/python examples/sdk-authoring/check_python_wheel.py /tmp/feral-sdk-artifacts/feral_sdk-0.1.0-py3-none-any.whl
npm --prefix sdk/node run build
cd sdk/node
npm pack --ignore-scripts --pack-destination /tmp/feral-sdk-artifacts
npm install --prefix /tmp/feral-sdk-node-consumer /tmp/feral-sdk-artifacts/feral-sdk-0.1.0.tgz --ignore-scripts --offline --no-audit --no-fund
FERAL_SDK_PACKAGE_DIR=/tmp/feral-sdk-node-consumer/node_modules/@feral/sdk node --test tests/plugin_runtime.test.cjs
```

The wheel check imports from the extracted artifact in an isolated fresh process
and deliberately blocks the core import. It uses dependencies already installed
in the pinned interpreter. The Node check installs only the locally built tarball
in a disposable consumer and invokes its host through the actual local runtime.
These are package/fresh-process checks, **not** a clean-clone dependency install,
production-process startup, clean-machine/native distribution or remote deployment
acceptance. Package installation from a signed registry and SDK publication are
separate gates. See [dated evidence](../../docs/roadmap/theora-personal-agent/SDK_PLUGIN_AUTHORING_EVIDENCE.md).
