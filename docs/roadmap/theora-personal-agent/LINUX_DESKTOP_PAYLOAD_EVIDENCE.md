# Linux desktop payload verification

October 3, 2026. Reuses the existing Tauri desktop shell and staging pipeline;
no separate Linux agent, memory store or UI is introduced.

The supported new staging profile is Linux x86_64 with glibc. It selects the
tracked lockfile's OpenCode1.18.10 baseline executable and verifies package
metadata, executable format/architecture and actual version before uv downloads
or staging mutations. Existing Darwin arm64/x86_64 mappings remain. Windows,
musl, Linux arm64 and unknown targets refuse this bounded staging profile.

The Ubuntu24.04 CI job installs locked dependencies, builds the actual Debian
package, checks package architecture/version, extracts it and relocates the
payload. Its acceptance command checks bundled Python version/FTS5/import
containment, OpenCode version and catalog executable, owned HTTP health, dashboard
assets, unchanged source/payload inventories and listener shutdown. It uses a
disposable same-root profile, no model provider and a null OS keyring backend.
Artifacts upload only after that command succeeds, as unsigned CI packages.

## Local verification

### First actual remote package result and bounded correction

Published `4eded179e2060c226739511fc53f5a8bde0eaa16` ran the actual Ubuntu
Debian build in [37131532547](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37131532547).
Package assembly succeeded; extracted-payload smoke failed. The original failure
receipt retained only `LinuxBundleError`, so its earliest failing guard is unknown.

The pinned Tauri builder and runtime use productName `FERAL` for the resource
directory `usr/lib/FERAL`; the native binary remains `usr/bin/feral-desktop`.
The smoke helper instead assumed the binary name for both paths. The correction
derives both identities from explicit trusted Tauri/Cargo metadata, refuses unsafe
or ambiguous components and resource-map drift, and retains the existing ELF,
containment, runtime, health, asset, inventory and shutdown guards.

Fixed allowlisted error codes and bounded phase/layout observations now survive
failure. No raw private exception or subprocess output is added to receipts.
**19 payload tests and seven platform tests pass**, along with targeted Ruff,
YAML and workflow shell syntax checks. Corrected actual-package acceptance has
not run. The original failed artifact and raw job log remain preserved.
Further Linux engineering is deferred while Mac delivery is prioritized.

### Earlier local-only verification

- **20 tests passed:**7 platform/staging-refusal tests and13 smoke-contract tests.
- Targeted configured Ruff, shell syntax and YAML trigger/job/permission checks
  passed. The actual cached Darwin arm64 package passed metadata, header and
  executable version probes.
- The smoke fixture exposed a canonical `/var` versus `/private/var` comparison
  error. Comparison roots now resolve consistently; the regression passes.
- No Linux executable download, Debian build or actual Linux payload execution
  occurred on the Mac. The new remote job must run before claiming that gate.

Local commands:

```sh
.venv/bin/python -m unittest discover -s scripts/tests -p test_desktop_bundle_platform.py -q
.venv/bin/python -m unittest discover -s scripts/tests -p test_linux_desktop_bundle_smoke.py -q
bash -n desktop/scripts/stage_bundle.sh
```

These checks do not establish Linux GUI/audio, clean-machine installation,
transactional update/rollback or a release signature. Mac Developer-ID and
notarization remain independent gates. The prior workflow's unsupported Windows
matrix does not constitute working Windows distribution and is not retained as
a passing lane.
