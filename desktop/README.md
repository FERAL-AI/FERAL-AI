# FERAL Desktop

FERAL uses a [Tauri 2](https://v2.tauri.app/) native host with a web-rendered interface. It is not a SwiftUI interface. The host manages the local brain, native windows, menus, tray, shortcuts and directory selection. The bundle carries Python, the brain and OpenCode so runtime startup does not require the user's Python, Node or a source checkout.

The current staging profile targets macOS on the build machine's architecture. macOS arm64 is the local verification target. Intel macOS, Linux, Windows and universal builds require separate packaging and verification; do not infer support from Tauri's available installer formats.

## Build prerequisites

- A current [Rust toolchain](https://rustup.rs/) and Xcode command line tools. Ensure `~/.cargo/bin` is on `PATH`.
- Node.js 20.19+ or 22.12+ for the Vite 8 build. Node is a build dependency, not a shipped runtime dependency.
- `bash`, `rsync` and `grep`.
- Network access for initial Rust dependencies, the pinned standalone CPython and Python packages.

From the ASOS repository root, install the pinned coding runtime:

```bash
npm install --prefix .tools/opencode opencode-ai@1.18.10 --no-audit --no-fund
```

Then build from `ASOS/desktop`:

```bash
npm install
npm run tauri:build -- --bundles app
```

`beforeBuildCommand` builds the dashboard and shell, then runs `npm run stage:bundle`. The macOS app is written to `src-tauri/target/release/bundle/macos/FERAL.app`. Use `--bundles dmg` for the DMG installer. The payload is large because it includes Python dependencies and the coding engine; measure the actual release artifact rather than relying on an old size estimate.

The existing `build:dmg` npm shortcut requests a universal target and is not the supported command for this single-architecture payload. A universal Rust shell alone does not make Python and OpenCode universal.

## Development

From `ASOS/desktop`:

```bash
npm install
npm run tauri:dev
```

Without a staged payload, runtime resolution can find the repository's `feral-core` and `.venv` by walking up from the executable. Run `make dev` at the ASOS root first. If staged resources are present, the native host prefers them; refresh staging when testing current source in a bundled configuration.

## Bundled runtime and relocation

`scripts/stage_bundle.sh` writes the gitignored `src-tauri/resources` tree:

| Bundle path | Purpose |
|---|---|
| `Contents/Resources/feral-core` | Brain source and built `webui_v2` dashboard. The host starts `python -m api.server` with this directory as cwd. Tests, caches, `.venv`, environment files and database/log files are excluded. |
| `Contents/Resources/python` | Relocatable python-build-standalone CPython pinned by ASOS `.python-pin`, with the brain's dependencies installed non-editable. |
| `Contents/Resources/opencode/bin/opencode` | OpenCode 1.18.10 native executable for the matching macOS architecture. Its license and version notice are staged alongside it. |

SQLite FTS5 is required for the memory store. The staging script probes FTS5 and module imports under the bundled interpreter instead of trusting a successful package installation. It uses a temporary `FERAL_HOME` so verification does not open the developer's live memory vault.

A copied virtualenv is not a standalone interpreter: its `pyvenv.cfg`, interpreter links and missing standard library can depend on the builder's machine. Staging therefore resolves managed CPython with `uv python find --managed-python --system --no-project`, verifies that `sys.base_prefix` and the standard library live inside the copied tree, and rejects virtualenvs and external symlinks.

The staging checks also require the built dashboard index and every referenced JavaScript/CSS asset, reject editable checkout references in site-packages, and verify that the brain reports the v2 dashboard. Refreshing an existing interpreter is conditional on both its version and its self-contained layout.

At runtime, the core is resolved from `FERAL_CORE_DIR`, bundled resources, then the bounded development checkout search. Python is resolved from `FERAL_PYTHON`, the bundle, then the development `.venv`; each candidate must pass its bounded FTS5 probe. Bare Python from the user's `PATH` is not selected.

The brain child's `PATH` places the resolved Python bin directory and executable bundled OpenCode directory before inherited entries. `FERAL_OPENCODE_BIN` points to the bundled executable when no explicit override exists. The `brain_runtime_info` command exposes resolved paths for diagnostics.

## Verification

From the ASOS root, verify the assembled app after building:

```bash
.venv/bin/python scripts/desktop_bundle_smoke.py desktop/src-tauri/target/release/bundle/macos/FERAL.app
```

This harness copies the actual `.app` into a different directory with spaces in its name, starts its bundled brain with isolated storage and an empty external `PATH`, verifies Python/FTS5/import paths, runs the bundled OpenCode version command without Node, and checks `/health` and the staged dashboard assets over HTTP. It terminates its own process group and checks that the listener closes. It disables model providers and synchronization for this payload test.

Before startup it rejects development vaults, environment files, coverage/cache artifacts, runtime databases and credential/key files in the core payload and the installed brain's source packages. Failure output lists filenames only. This check is an additional guard against excluded files surviving from an older staging run.

The harness tests packaged payload startup, not native GUI startup, the native Quit menu, OS privacy prompts or model/tool behavior. Native lifecycle unit tests exercise owned-child termination, refusal to kill unrelated PIDs and process-group cleanup. Native GUI startup and shutdown need their own assembled-app verification. Linux remains untested by this macOS harness. Record actual commands and outcomes with each artifact.

The native host drains bounded brain logs and uses bounded interpreter and health probes. On Unix it starts the brain in an owned process group, requests graceful termination and escalates after a deadline. Main-window destruction and application exit clean up that brain; closing an auxiliary window does not.

Native readiness also requires a live owned brain process and its per-spawn identity header on `/health`. A different server occupying the configured port cannot make the shell report ready. This marker identifies the process instance; it is not an authentication credential.

## Signing and updates

The committed signing identity is currently unset and updater artifacts are disabled. This configuration does not establish a Developer ID signed, notarized public release or an automatic updater.

For a local ad-hoc build, Tauri accepts `APPLE_SIGNING_IDENTITY=-`. Ad-hoc signing does not give downloaded apps trusted Developer ID status; Gatekeeper may require explicit approval. See [Tauri macOS signing](https://v2.tauri.app/distribute/sign/macos/).

A public Mac release requires a valid Developer ID Application identity, signing of nested executable code, notarization and stapling. Python executables, native extensions/dynamic libraries and OpenCode all need review in the final artifact. Sign nested code before the outer app and verify the assembled result; [Apple's code signing guidance](https://developer.apple.com/library/archive/technotes/tn2206/_index.html) describes these constraints. Hardened runtime compatibility must be tested with the actual Python wheels and OpenCode engine before selecting any exception entitlements. Do not assume a locally working ad-hoc binary will pass that release gate.

`Info.plist` contains microphone and camera purpose strings. These describe access; they do not prove OS permission prompts, capture or voice behavior work in the final signed app.

Automatic updates need a separately configured updater implementation, trusted public key, private-key handling and signed update artifacts. Updater signatures and Apple notarization serve different purposes. See [Tauri updater documentation](https://v2.tauri.app/plugin/updater/).

## Theming

`feral-client-v2/src/styles/tokens.css` is the source of truth. `scripts/sync_tokens.sh` generates the desktop's `src/tokens.css`; `npm run build` and `npm run dev` refresh it through their pre-hooks. Use `npm run check:tokens` to check drift. Edit the source token file, not the generated copy.
