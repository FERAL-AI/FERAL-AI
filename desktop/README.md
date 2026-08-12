# FERAL Desktop

Native desktop app wrapping the FERAL web UI via [Tauri 2](https://v2.tauri.app/).

Unlike a plain shell, this app **carries its own Python runtime and its own copy of the brain**, so it can start the brain on a machine that has no FERAL checkout and no suitable interpreter.

## Requirements

- [Rust toolchain](https://rustup.rs/) (1.77+)
- Node.js 18+
- Tauri CLI: `cargo install tauri-cli`
- Platform build tools (Xcode on macOS, Visual Studio on Windows)
- `bash`, `rsync` and `curl` on the build machine, for the payload staging step
- Network access on the first build, to fetch the pinned CPython

## Development

```bash
npm install
npm run tauri:dev
```

This starts both the Vite dev server and the Tauri window. In a dev build the app has no staged payload, so it falls back to the repo: it walks up from the executable to find `feral-core/`, and uses the repo's `.venv/bin/python` (run `make dev` at the repo root first).

## Build

```bash
npm run tauri:build
```

`beforeBuildCommand` runs `npm run stage:bundle` for you, which is `scripts/stage_bundle.sh`. Run it by hand any time you want to refresh the payload.

Produces:
- macOS: `src-tauri/target/release/bundle/dmg/FERAL_*.dmg`
- Linux: `src-tauri/target/release/bundle/appimage/FERAL_*.AppImage`
- Windows: `src-tauri/target/release/bundle/msi/FERAL_*.msi`

`--bundles app` builds just the macOS `.app` and is much faster when you only need to test.

The bundled payload is large: ~110MB of brain source plus a ~347MB interpreter, so the macOS `.app` is around 517MB. That is the cost of not depending on the user's Python.

## The bundled runtime

`scripts/stage_bundle.sh` writes `src-tauri/resources/` (gitignored):

| Path in the bundle | What it is |
|---|---|
| `Contents/Resources/feral-core` | Brain source. The app runs `python -m api.server` with cwd set here. `build/`, `tests/` and caches are excluded. |
| `Contents/Resources/python` | Relocatable python-build-standalone CPython at the version in the repo's `.python-pin`, with `feral-core[llm]` installed non-editable. |

**Why bundle an interpreter at all.** FERAL's SQLite needs FTS5 (`MemoryStore` and `KnowledgeGraph` create five `CREATE VIRTUAL TABLE ... USING fts5` tables while being constructed, so without it the brain does not degrade, it does not start) and it benefits from loadable extensions, which gate `sqlite-vec`. Interpreters commonly ship one and not the other. Measured on macOS arm64:

| Interpreter | SQLite | FTS5 | loadable extensions |
|---|---|---|---|
| pyenv 3.11.11 (macOS default build) | 3.51.0 | yes | no |
| python-build-standalone 3.11.13 | 3.49.1 | **no** | yes |
| python-build-standalone 3.11.15 (pinned) | 3.53.1 | yes | yes |

A GUI app cannot ask a user to audit their interpreter's compile flags, so it brings one whose flags are known. The staging script probes the staged interpreter and imports the brain under it before declaring success: a bundled interpreter without FTS5 produces an app that installs cleanly, launches cleanly, and whose health indicator simply never turns green.

The pin is shared with the development environment (`.python-pin` at the repo root), so the app and `make dev` cannot drift onto different interpreters.

## How the brain is located at run time

Everything is resolved when the app runs, never at compile time. The previous implementation used `env!("CARGO_MANIFEST_DIR")`, which rustc expands during the build, so the shipped binary carried the *build machine's* source path and `start_brain` failed everywhere else; and it spawned bare `python3` from the user's `PATH`, an interpreter it knew nothing about.

**feral-core**, first match wins:

1. `FERAL_CORE_DIR` environment variable.
2. `Contents/Resources/feral-core` (or the platform equivalent resource directory).
3. An upward walk from the running executable, bounded to 8 levels, looking for `feral-core/api/server.py`. This is what makes `cargo run` and dev builds work.

**Interpreter**, first match that passes a capability probe:

1. `FERAL_PYTHON` environment variable.
2. `Contents/Resources/python/bin/python3` (`python.exe` on Windows).
3. The repo's `.venv/bin/python`, for dev builds.

`PATH` is never consulted. Each candidate is run with a one-line FTS5 probe before it is used, and if none passes, `start_brain` returns an error naming every candidate it tried and why each was rejected, instead of spawning a process that dies during boot.

The `brain_runtime_info` Tauri command returns the resolved paths, for a troubleshooting panel.

## Architecture

The app loads the brain's web UI in a native window and manages the brain process itself (`start_brain` / `stop_brain`), so you do not need a separate `feral serve`.

Features:
- System tray icon (click to show/hide)
- Auto-detect brain health
- Native window with full webcam, voice, and tool access

## Known gaps

- `tauri build` with updater artifacts enabled ends in `A public key has been found, but no private key` unless `TAURI_SIGNING_PRIVATE_KEY` is set. The app bundle is produced before that step, so the artifact is usable, but a release build needs the signing key.
- The staging script is `bash` + `rsync`. On Windows, run it under Git Bash or WSL before `tauri build`.
