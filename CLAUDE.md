# CLAUDE.md — FERAL / ASOS

Context for agents working in this repo. Read `AUDIT-FIXES.md` next if you were sent here to fix defects.

## What this is

FERAL is a local-first AI runtime that users install on their own machine (macOS 13+ / Linux, Python 3.11+). It orchestrates LLM providers, keeps a 4-layer memory store, drives hardware over a WebSocket protocol (HUP), and emits server-driven UI ("Gen-UI" / SDUI) that clients render.

Version `2026.8.8`. Public beta. Single-user local deployment is the only supported target.

## Layout

| Path | Language | What it is |
|---|---|---|
| `feral-core/` | Python | The brain. ~170k production lines, ~130k test lines. Everything below is under here unless stated. |
| `feral-core/agents/` | Python | Orchestrator (`orchestrator.py`), LLM router (`llm_provider.py`, hand-rolled httpx to 16 providers) |
| `feral-core/api/` | Python | FastAPI app. `server.py` is the entrypoint, `state.py` holds the singleton |
| `feral-core/memory/` | Python | aiosqlite store, embeddings, knowledge graph, CRDT federated sync |
| `feral-core/models/protocol.py` | Python | **Canonical wire schemas.** `HUP_VERSION` lives at line 15 |
| `feral-core/cli/` | Python | `feral setup / start / doctor / memory / sync / access` |
| `feral-core/voice/`, `perception/` | Python | STT/TTS routing, VAD, wake word, sensor fusion |
| `feral-client-v2/` | React (JSX) | Current web client. `feral-client/` is the superseded v1 |
| `feral-nodes/` | Python, TS, Swift, Kotlin | Device SDKs and daemons. `HUP_SPEC.md` is the protocol source of truth |
| `feral-registry/`, `feral-relay/` | Python | App registry; NAT-traversal relay |
| `desktop/` | Rust (Tauri) | Experimental shell. Bundles its own CPython + a copy of `feral-core` and spawns `python -m api.server` against them; see "The interpreter" below |

## Commands

```bash
make dev                                  # build pinned .venv, install feral-core[all,dev] + client deps
make test                                 # cd feral-core && python -m pytest tests/ -v
make serve                                # feral serve
make doctor                               # feral doctor — reports real runtime state
```

`make dev` installs into `.venv/` at the repo root, built from `.python-pin`, and every other target routes through it. It needs `uv >= 0.12` and fetches a repo-local one if your machine has none. See below.

CI equivalents (what actually gates a PR):

```bash
cd feral-core && ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .
cd feral-core && pip install --constraint requirements.lock -e ".[all,dev]"
cd feral-core && python -m pytest tests/ --cov --cov-fail-under=50
```

`make lint` does **not** lint — it runs pytest and prints a note. Use the ruff line above.

## The interpreter: pinned for dev, bundled for users

This is the single most load-bearing environment fact in the repo. Read it before you debug anything that looks like "memory is broken on my machine".

### The two SQLite features, and why they are separate

FERAL's SQLite needs two independent build-time features. Stock interpreters routinely ship one and not the other, and the consequences are not symmetric.

| Interpreter | SQLite | FTS5 | loadable extensions |
|---|---|---|---|
| pyenv 3.11.11 (macOS default build) | 3.51.0 | **yes** | no |
| python-build-standalone 3.11.13 | 3.49.1 | **no** | yes |
| **python-build-standalone 3.11.15 (pinned)** | 3.53.1 | **yes** | **yes** |

**FTS5 is required.** `memory/store.py` and `memory/knowledge_graph.py` create five `CREATE VIRTUAL TABLE ... USING fts5` tables during construction. Without it the store does not degrade, the brain does not start. It used to die as `sqlite3.OperationalError: no such module: fts5` pointing at a triple-quoted SQL string; `memory/sqlite_features.require_fts5` now raises `SQLiteFeatureError` naming the interpreter, the SQLite version and the fix, before any DDL runs so no half-created database is left behind.

**Loadable extensions are optional.** They gate `sqlite-vec`. pyenv's macOS default omits `--enable-loadable-sqlite-extensions`, so `sqlite3.Connection` has no `.enable_load_extension` at all, while `pip install sqlite-vec` and `import sqlite_vec` both still succeed. `sqlite_vec_available()` returns False, logs at INFO, and the vector leg runs over numpy. Per F-17 that numpy path is the **faster** of the two (0.46ms vs 7.08ms for top-5 over 12k 384-dim vectors), so this costs resident memory and nothing else. Never prescribe an interpreter rebuild as a remedy for it.

**Neither feature implies the other.** 3.11.13 has extensions and no FTS5; pyenv 3.11.11 has FTS5 and no extensions. Anything that checks one and infers the other is wrong. `memory/sqlite_features.py` is the single place both are probed, and `feral doctor` renders them as two separate rows with two different severities (`SQLite FTS5` is a `_fail`, `SQLite loadable extensions` is an `_info`).

### Development: `.python-pin` and `make dev`

From a clean clone, one command:

```bash
make dev
```

That fetches a uv new enough to reach the pin (repo-local, under `.uv/`, if your system uv is too old), installs CPython 3.11.15 from python-build-standalone, creates `.venv/`, installs `feral-core[all,dev]` with `--constraint feral-core/requirements.lock` (the same extras and constraint CI uses, so a local run and a CI run agree), and finishes by printing the real feature report:

```
  interpreter : /path/to/.venv/bin/python (Python 3.11.15)
  sqlite      : 3.53.1
  fts5        : OK
  loadable ext: OK
  Environment verified.
```

If FTS5 is not OK, `make dev` **fails**. It does not print a warning and continue into an environment where the brain cannot boot.

Other targets: `make dev-reset` (delete `.venv` and rebuild), `make dev-verify` (print the report for whatever interpreter is current), `make clean-uv` (drop the repo-local uv).

**`.python-pin`, not `.python-version`.** pyenv reads `.python-version`, and a repo-root `.python-version` naming a version pyenv does not have does not fail loudly, it makes pyenv's shims fall through. Every bare `python3`, `ruff`, `pytest` and `pip` run anywhere inside the tree then silently becomes some other interpreter. Measured in this repo while a `.python-version` pinning 3.11.15 was present:

```
$ python3 -c "import aiosqlite"        # inside the repo
ModuleNotFoundError: No module named 'aiosqlite'
$ python3 -c "import sys; print(sys.executable)"
/opt/homebrew/opt/python@3.14/bin/python3.14      # not 3.11 anything
$ ruff --version
pyenv: version `3.11.15' is not installed ... pyenv: ruff: command not found   # exit 127
```

After removing it, both resolve normally again. `.python-pin` is read only by this repo's own tooling (`Makefile`, `scripts/ensure_uv.sh`, `desktop/scripts/stage_bundle.sh`), so nothing on `PATH` is hijacked. `.python-version` is in `.gitignore` and `make dev` refuses to run while one exists.

**uv >= 0.12 is required, not cosmetic.** uv resolves versions against a manifest baked into its own binary. Every 3.11 that uv 0.7.x can reach is from the pbs generation that shipped FTS5 off; 3.11.15 needs a uv that knows pbs release `20260807`. `scripts/ensure_uv.sh` prefers your system uv when it is new enough and otherwise downloads a pinned 0.12.3 into `.uv/`, leaving your global uv alone.

**Escape hatch.** `PYTHON=/path/to/python make dev` skips the pin entirely and says so. On that path an FTS5 failure is a warning rather than an error, because someone who named their own interpreter has been told.

### End users: the desktop bundle

`desktop/` ships its own interpreter. `desktop/scripts/stage_bundle.sh` (run automatically by `tauri build`, or by hand with `npm run stage:bundle`) stages into `desktop/src-tauri/resources/`:

- `feral-core/`: the brain source, because the app starts it as `python -m api.server` with cwd set here. `build/`, `tests/` and caches are excluded; shipping `feral-core/build/lib/` would put a stale second copy of `agents/`, `api/` and `memory/` on the path (trap 1 below).
- `python/`: a relocatable python-build-standalone CPython at the version in `.python-pin`, with `feral-core[llm]` installed **non-editable** (an editable install writes the build machine's path into a `.pth`).

The script verifies FTS5 on the staged interpreter and imports the brain under it before declaring success, because a bundled interpreter without FTS5 produces an app that installs, launches, and whose health dot simply never turns green. Payload is ~460MB (110MB core, 347MB interpreter), which is what makes the resulting `.app` ~517MB.

`desktop/src-tauri/src/main.rs` resolves both at **run** time. It used to use `env!("CARGO_MANIFEST_DIR")`, a compile-time constant, so the shipped binary carried the build machine's source path and `start_brain` returned Err on every other machine; and it spawned bare `python3` from the user's PATH, which is an interpreter the app knows nothing about. Now:

- feral-core: `FERAL_CORE_DIR` → `Contents/Resources/feral-core` → an upward walk from the executable (bounded to 8 levels) looking for `feral-core/api/server.py`.
- interpreter: `FERAL_PYTHON` → `Contents/Resources/python/bin/python3` → the repo's `.venv` for dev builds. Every candidate is capability-probed for FTS5 before use, and PATH is never consulted.
- `brain_runtime_info` (Tauri command) reports what was resolved.

### What is still not fixed

`pip install feral-ai` end users still supply their own interpreter. They now get `SQLiteFeatureError` with a remedy instead of a raw sqlite error, and `feral doctor` names the problem, but nothing installs a working interpreter for them. `make dev` also does nothing for anyone who bypasses it.

## Traps that will waste your time

**1. `feral-core/build/lib/` is a complete duplicate of the source tree.** 404 `.py` files shadowing the real 949. On macOS, BSD `grep` prints paths *without* a leading `./`, so the natural exclusion `/build/` matches nothing and every count runs ~38% high. Use `^build/` or absolute paths:

```bash
# WRONG — silently includes the duplicate tree
grep -rn "pattern" --include=*.py . | grep -v "/build/"

# RIGHT
grep -rn "pattern" --include=*.py . | grep -vE '^build/|^dist/'
```

Also: `grep -r` launched from the repo root does *not* descend into `feral-core/build`, so root-level totals are wrong in both directions. Prefer `find … -print0 | xargs -0 grep`.

For tools, this trap is worse than a skewed count — it takes them to zero. `mypy` without `--exclude '^build/'` does not degrade, it **refuses to start**: `Duplicate module named "agents"`. Any tool that resolves modules by name will do the same.

**2. A git repo exists at `$HOME`.** Running git from an unexpected directory can resolve to it and report a completely different working tree. Always use `git -C /Users/mahmoudomar/Desktop/thoera-mac/ASOS …` or confirm with `git rev-parse --show-toplevel`.

**3. Tests can pass while production is broken.** Test doubles in `tests/` have drifted from real signatures — that is the root cause of F-01 in `AUDIT-FIXES.md`. A green suite is not evidence a call site works. Check the real definition.

**4. There is no type checker configured.** `mypy` at default settings reports 324 errors in 103 files. Nothing runs it. Annotations exist (92.5% of params) but are unverified, so trust the code, not the annotation.

**5. `ruff` runs on `--select=E,F,W` minus six ignores** — roughly 1.5% of its rules, with `F401` (unused imports) disabled. A clean ruff run means very little.

**6. Only `feral-core` is in CI.** `feral-registry`, `feral-nodes`, `feral-relay`, `scripts`, `sdk/python`, `packages` (102 Python files total) have zero lint and zero tests. The TypeScript, Swift, and Kotlin SDKs are never compiled by CI at all.

## Conventions

- **`models/protocol.py` is canonical.** Never hard-code a wire constant that exists there. `HUP_VERSION` has already shipped a three-way mismatch; there is now an AST guard, but it only covers `api/server.py`.
- **Version literals are synced by `scripts/sync_versions.py`** across 17 file+regex locations and gated by `.github/workflows/version-coherence.yml`. Run `python scripts/sync_versions.py --check` after touching any version.
- **Do not add a bare `except Exception: pass`.** There are already 1,702 broad handlers and ~210 silent swallows; they are the repo's dominant defect class. If you must catch broadly, log with context and re-raise or return a typed error.
- **Async discipline:** never call blocking I/O inside `async def`. Use `asyncio.to_thread` (85 existing call sites) or the async-native API. `subprocess.run` inside a route handler is a bug.
- **Background tasks must be referenced.** Use `state.register_background_task(...)` or hold the task in a `set[asyncio.Task]` with an `add_done_callback(the_set.discard)` so it stays bounded (see `agents/orchestrator.py:210-218`). A bare `asyncio.create_task(...)` **or `asyncio.ensure_future(...)`** can be garbage-collected mid-flight; the loop holds tasks only weakly. `tests/test_background_task_references.py` AST-scans for both and will fail the build on a new one.
- **No em dashes** in code, comments, commit messages, or docs.

## Where the truth is written down

- `CHANGELOG.md` (472KB) is a genuine forensic record — release notes include root-cause analysis. Grep it before assuming a bug is new.
- `AUDIT-r13/` and `AUDIT-r14/` are prior internal audits with `file:line` citations. `AUDIT-r14/round3/SCOREBOARD-r3.md` scores 21 areas; none reach 5/5.
- `feral-nodes/HUP_SPEC.md` is the device protocol spec, and it is **prose** — every SDK implements it by hand, which is why cross-language drift is a recurring bug class.
