# FERAL Desktop

Native desktop app wrapping the FERAL web UI via [Tauri 2](https://v2.tauri.app/).

Unlike a plain shell, this app **carries its own Python runtime and its own copy of the brain**, so it can start the brain on a machine that has no FERAL checkout and no suitable interpreter.

## Requirements

- [Rust toolchain](https://rustup.rs/) (1.77+). rustup installs to `~/.cargo/bin`, and it is not always on `PATH` in a non-login shell. If `cargo` is "command not found", `export PATH="$HOME/.cargo/bin:$PATH"` first; every `cargo` line in this file assumes that.
- Node.js 18+
- The Tauri CLI comes from `devDependencies` (`@tauri-apps/cli`), so `npm install` provides it. A global `cargo install tauri-cli` is not required.
- Platform build tools (Xcode on macOS, Visual Studio on Windows)
- `bash`, `rsync` and `grep` on the build machine, for the payload staging step
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

The bundled payload is large: ~110MB of brain source plus a ~415MB interpreter (a 78MB CPython plus `feral-core[llm]`'s dependencies), so the macOS `.app` is around 525MB. That is the cost of not depending on the user's Python.

## The bundled runtime

`scripts/stage_bundle.sh` writes `src-tauri/resources/` (gitignored):

| Path in the bundle | What it is |
|---|---|
| `Contents/Resources/feral-core` | Brain source, including the built `webui_v2/` dashboard. The app runs `python -m api.server` with cwd set here. `build/`, `tests/`, caches and `.venv/` are excluded. |
| `Contents/Resources/python` | Relocatable python-build-standalone CPython at the version in the repo's `.python-pin`, with `feral-core[llm]` installed non-editable into the interpreter's own `site-packages`. |

**Why bundle an interpreter at all.** FERAL's SQLite needs FTS5 (`MemoryStore` and `KnowledgeGraph` create five `CREATE VIRTUAL TABLE ... USING fts5` tables while being constructed, so without it the brain does not degrade, it does not start) and it benefits from loadable extensions, which gate `sqlite-vec`. Interpreters commonly ship one and not the other. Measured on macOS arm64:

| Interpreter | SQLite | FTS5 | loadable extensions |
|---|---|---|---|
| pyenv 3.11.11 (macOS default build) | 3.51.0 | yes | no |
| python-build-standalone 3.11.13 | 3.49.1 | **no** | yes |
| python-build-standalone 3.11.15 (pinned) | 3.53.1 | yes | yes |

A GUI app cannot ask a user to audit their interpreter's compile flags, so it brings one whose flags are known.

The pin is shared with the development environment (`.python-pin` at the repo root), so the app and `make dev` cannot drift onto different interpreters.

### An interpreter, not a virtualenv

This distinction is the whole reason step 2 of the staging script is written the way it is. A python-build-standalone **installation** is relocatable: its standard library sits under its own `lib/`, and its internal symlinks (`bin/python3` to `bin/python3.11`) are relative. A **virtualenv** is not relocatable at all: it has a `pyvenv.cfg` naming an absolute `home =`, its `bin/python` is a symlink to that absolute path, and its `lib/pythonX.Y/` contains only `site-packages`, never the standard library.

Copy a virtualenv into an `.app` and the payload's interpreter loads `os`, `encodings` and everything else from a directory under the *build machine's* home. On that machine every check passes, for exactly that reason. On any other machine the interpreter cannot start, `resolve_python` rejects it, and `start_brain` returns an error: the app installs, launches, and its health dot never turns green.

That had shipped. `uv python find "$PIN"` resolves the ambient project environment before the managed installation, so run from the repo root it answered `$REPO_ROOT/.venv/bin/python3` and the script staged the development virtualenv. The staging script now asks for `uv python find --managed-python --system --no-project`, and, more importantly, verifies the result instead of trusting it.

### What the staging script proves before it declares success

Each of these is REQUIRED and fails the build. None of them is a warning.

| Check | The failure it prevents |
|---|---|
| `webui_v2/index.html` is staged | An app that starts, answers `/health`, goes green, and serves `api/server.py`'s packaging-fault page where the dashboard belongs. |
| Every `assets/*.js` and `*.css` the built `index.html` references is staged | A stale bundle whose index points at asset hashes that no longer exist: a blank window. |
| The staged interpreter is self-contained (`sys.base_prefix` and the standard library both resolve inside the payload; no `pyvenv.cfg`) | The virtualenv defect above. |
| No symlink in the payload resolves outside it | A build-machine absolute path that dangles on the user's disk. |
| No `.pth` or `__editable__` module in `site-packages` names the build machine's checkout | An editable install leaking in, so the app imports the builder's source tree when that path happens to exist and fails opaquely when it does not. |
| The staged interpreter has FTS5 | The brain aborts during `MemoryStore` construction. |
| The brain's modules import under the staged interpreter | `pip install` succeeding says nothing about whether the wheels load. |
| The imported brain reports web UI variant `v2` | Files being present and the brain agreeing they are servable are different assertions, and only the second is what the user sees. |

Reuse is an optimisation and is only taken when the already-staged interpreter is both at the pinned version and self-contained. Matching on version alone is what let the broken virtualenv survive every subsequent build: it reported 3.11.15, so restaging was skipped and the fault was inherited.

The probe that imports the brain redirects `FERAL_HOME` at a throwaway directory. This is not cosmetic: importing `api.server` constructs real subsystems, and against the default `~/.feral` it opened the developer's live vault and rebuilt an FTS index in their real `memory.db`. A build step must not touch the operator's data.

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

## Theming

Colours come from the canonical FERAL design system. The source of truth is `feral-client-v2/src/styles/tokens.css` and nothing in this app is allowed to restate a colour it defines.

The desktop app is a separate Vite package and cannot import across the package boundary, so `scripts/sync_tokens.sh` copies the token file to `src/tokens.css` with a generated header. `npm run build` runs it via a `prebuild` hook, and `npm run dev` via `predev`, so `tauri:build` and `tauri:dev` both pick up a token change without anyone remembering to. The copy is committed, which keeps `vite build` working in a tree without `feral-client-v2` and makes drift show up as a diff in review. If the source file is missing the script fails rather than skipping, because a skipped sync is the drift it exists to prevent.

- `npm run sync:tokens` refreshes the copy by hand.
- `npm run check:tokens` fails if the committed copy is stale, for CI.

`src/tokens.css` is generated. Edit `feral-client-v2/src/styles/tokens.css` instead.

The desktop shell's own role names (`--bg`, `--surface`, `--border`, `--text`, `--muted`, `--accent`, `--accent2`, `--glow`, `--danger`) are defined once, in the `:root` block in `index.html`, and every one of them is an alias onto a `--v2-*` token. `src/main.js` and `floating-window.html` (at the package root, next to `index.html`, because Vite needs both HTML entry points there) read those variables and contain no colour literals. `<html>` carries `class="v2-dark"`, which pins the token file to its dark ramp: this chrome is only the loading, setup and error screens, so there is no light variant to switch to.

## Architecture

The app loads the brain's web UI in a native window and manages the brain process itself (`start_brain` / `stop_brain`), so you do not need a separate `feral serve`.

Features:
- System tray icon (click to show/hide)
- Auto-detect brain health
- Native window with full webcam, voice, and tool access

### Health probing is bounded

The tray tooltip is driven by a loop that probes `<brain>/health` every 2 seconds on its own thread, and the UI calls the same probe through `check_brain_health`. Both go through `probe_health_at`, which uses a client with a 1 second connect timeout and a 2 second total timeout.

The timeouts are the point. `reqwest::blocking::get` has none by default, so a brain that accepts the TCP connection and then never answers (a python wedged mid-boot, a stopped process still holding the port) parked that thread permanently. The tooltip then froze on its last value, which for a brain that came up and then hung is the green dot: the one indicator a user has, stuck reporting health because of the failure. A silent brain is now reported as "no answer within 2s", which is a different message from "unreachable" so the two cases stay distinguishable.

The interpreter capability probe is bounded the same way, by `PROBE_TIMEOUT` (10s). It runs on the path taken by the button that starts FERAL and by the diagnostics panel a user opens when something is already wrong, and `Command::output()` waits forever.

## Known gaps

- `tauri build` with updater artifacts enabled ends in `A public key has been found, but no private key` unless `TAURI_SIGNING_PRIVATE_KEY` is set. The app bundle is produced before that step, so the artifact is usable, but a release build needs the signing key.
- The staging script is `bash` + `rsync`. On Windows, run it under Git Bash or WSL before `tauri build`.
- **Nothing builds this app automatically.** `.github/workflows/desktop.yml` is `workflow_dispatch:` only, so a change to `src-tauri/` compiles in no CI run until somebody dispatches it by hand. `npm run check:tokens` exists and is not called by any workflow either; the committed `src/tokens.css` was found 73 lines stale against its source of truth, which only surfaced because `npm run build` regenerates it.
- `stage_bundle.sh` verifies the payload as staged in `src-tauri/resources/`. It does not verify what Tauri then copies into the `.app`, which is a separate step with its own behaviour (the bundler dereferences symlinks, so the three `bin/python*` links become three full copies of a 17MB binary). Verifying the assembled `.app` is not covered.
