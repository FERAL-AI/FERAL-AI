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
| `feral-client-v2/` | React (JSX) | The web client. The superseded v1 at `feral-client/` was deleted in 2026.8.12; its built bundle survives as `feral-core/webui/`, served only when `FERAL_SERVE_LEGACY_WEBUI=1` and `webui_v2/` is absent, and nothing can rebuild it |
| `feral-nodes/` | Python, TS, Swift, Kotlin | Device SDKs and daemons. `HUP_SPEC.md` is the protocol source of truth |
| `feral-registry/`, `feral-relay/` | Python | App registry; NAT-traversal relay |
| `desktop/` | Rust (Tauri) | Experimental shell. Bundles its own CPython + a copy of `feral-core` and spawns `python -m api.server` against them; see "The interpreter" below |

## Commands

```bash
make dev                                  # build pinned .venv, install feral-core[all,dev] + client deps
make test                                 # both suites: test-py + test-client
make test-py                              # cd feral-core && python -m pytest tests/ -q --no-cov  (~6 min)
make test-client                          # cd feral-client-v2 && npm test  (138 files / 1067 tests)
make e2e                                  # cd feral-client-v2 && npm run e2e  (7 spec files / 30 tests)
make e2e-real-brain                       # the same browser against a LIVE brain, nothing stubbed
make lint                                 # ruff, the exact ruleset CI gates on
make serve                                # feral serve
make doctor                               # feral doctor — reports real runtime state
```

`make dev` installs into `.venv/` at the repo root, built from `.python-pin`, and every other target routes through it. It needs `uv >= 0.12` and fetches a repo-local one if your machine has none. See below.

`make lint` **does** lint. It used to run pytest with `2>/dev/null || true` and print a note, so it reported success unconditionally and linted nothing; it now runs the ruff invocation below verbatim, and a green `make lint` and a green CI lint job mean the same thing.

`make test` **does** run both suites. It used to run only the Python side, so changing a page and running `make test` gave a green result that had not executed one line of the change.

`make e2e` needs the Playwright browser, which `npm install` does not fetch. `make dev-deps` downloads chromium; the target fails loudly rather than silently if it is missing, and builds `dist/` for you if it is absent.

`make e2e` runs against `vite preview` with `**/api/**` stubbed per-spec. That server answers `index.html` for **any** path at status 200 and hosts no API, so four defect classes are structurally invisible to it: a route the brain does not serve, a JSON fetch answered with HTML (how the Skills page shipped broken), a `/api/*` path the client calls that no router registers, and a control that reports success while its request failed. `make e2e-real-brain` is the other half: it boots a brain on a throwaway `FERAL_HOME`, bundles the client into `feral-core/webui_v2` so the brain serves the code under test, then runs `e2e/real_brain_pages.spec.ts` and `e2e/real_brain_controls.spec.ts` against it with **nothing stubbed**. Those two specs skip themselves unless `FERAL_E2E_REAL_BRAIN=1` and `FERAL_E2E_URL` are both set, so `make e2e` and the required CI gate are unaffected. In CI they run from the opt-in `.github/workflows/v2-real-brain-e2e.yml` (manual dispatch, the `real-brain-e2e` PR label, or nightly). The control walk clicks by blocklist, so it **mutates** the brain it points at; never aim it at `~/.feral`.

CI equivalents (what actually gates a PR):

```bash
# lint job — ruff's version bound comes from feral-core/pyproject.toml [dev],
# which is also what `make lint` uses. CI reads the bound out of the manifest
# rather than restating it.
cd feral-core && ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .

# install, both pytest jobs
cd feral-core && pip install --constraint requirements.lock -e ".[all,dev]"

# PR fast lane (brain-tests-pr): skips tests/perf, 60s per-test ceiling
cd feral-core && python -m pytest tests/ -p no:randomly -v --cov --cov-fail-under=50 \
  --ignore=tests/perf --timeout=60 --timeout-method=thread

# push-to-main matrix (brain-tests): 3.11 + 3.12, includes tests/perf, 300s ceiling
cd feral-core && python -m pytest tests/ -p no:randomly -x --cov --cov-fail-under=50 \
  --timeout=300 --timeout-method=thread

# client-v2 vitest coverage gate (thresholds live in vitest.config.js)
cd feral-client-v2 && npm run test:coverage

# client-v2 playwright e2e (job `client-v2-e2e`, REQUIRED)
cd feral-client-v2 && npm ci && npm run e2e:install && npm run build && npm run e2e
```

`--strict-markers` is on in `[tool.pytest.ini_options]`. An unrecognised or undeclared marker is a collection **error**, not a warning, so `markers` in `pyproject.toml` describes what actually exists. Declare a new marker there before using it.

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

- `feral-core/`: the brain source **and the built `webui_v2/` dashboard**, because the app starts it as `python -m api.server` with cwd set here. `build/`, `tests/`, caches and `.venv/` are excluded; shipping `feral-core/build/lib/` would put a stale second copy of `agents/`, `api/` and `memory/` on the path (trap 1 below).
- `python/`: a relocatable python-build-standalone CPython at the version in `.python-pin`, with `feral-core[llm]` installed **non-editable** into the interpreter's own `site-packages` (an editable install writes the build machine's path into a `.pth`).

**A python-build-standalone installation, never a virtualenv.** This is the load-bearing distinction and it was got wrong in a shipped build. A pbs install is relocatable: stdlib under its own `lib/`, internal symlinks relative. A venv is not relocatable in any sense: `pyvenv.cfg` names an absolute `home =`, `bin/python` is a symlink to that absolute path, and `lib/pythonX.Y/` holds only `site-packages` with no stdlib at all. `uv python find "$PIN"` resolves the ambient project environment *before* the managed install, so run from the repo root it answers `$REPO_ROOT/.venv/bin/python3`, and the script staged the development virtualenv. Measured on the `.app` in `desktop/src-tauri/target/release/bundle/` at the time this was found, the shipped interpreter's `os.__file__` was `/Users/<builder>/.local/share/uv/python/cpython-3.11.15-.../lib/python3.11/os.py`. That is the `CARGO_MANIFEST_DIR` defect reintroduced in the payload instead of the binary. Use `uv python find --managed-python --system --no-project`, and installing into the result needs `uv pip install --break-system-packages` because uv marks its managed installs externally managed and suggests a venv, which is the one thing that cannot be bundled.

The script now fails the build (never warns) unless: the v2 dashboard and every asset its `index.html` references are staged; the staged interpreter is self-contained (`sys.base_prefix` and the stdlib both resolve inside the payload, no `pyvenv.cfg`); no symlink in the payload resolves outside it; no `.pth` names the build machine's checkout; FTS5 works; the brain's modules import; and the imported brain reports web UI variant `v2`. Reuse of an already-staged interpreter is gated on self-containment as well as version, because matching on version alone is what let the broken venv survive every subsequent build. Payload is ~525MB (110MB core, 415MB interpreter: 78MB CPython plus dependencies).

The failure mode all of this is designed against is a single symptom with many causes: an app that installs, launches, and whose health dot never turns green, or turns green over a brain with no dashboard behind it.

`desktop/src-tauri/src/main.rs` resolves both at **run** time. It used to use `env!("CARGO_MANIFEST_DIR")`, a compile-time constant, so the shipped binary carried the build machine's source path and `start_brain` returned Err on every other machine; and it spawned bare `python3` from the user's PATH, which is an interpreter the app knows nothing about. Now:

- feral-core: `FERAL_CORE_DIR` → `Contents/Resources/feral-core` → an upward walk from the executable (bounded to 8 levels) looking for `feral-core/api/server.py`.
- interpreter: `FERAL_PYTHON` → `Contents/Resources/python/bin/python3` → the repo's `.venv` for dev builds. Every candidate is capability-probed for FTS5 before use, and PATH is never consulted.
- `brain_runtime_info` (Tauri command) reports what was resolved.

Both the health probe and the interpreter capability probe are **time-bounded**. `reqwest::blocking::get` and `Command::output()` each wait forever by default, and the health probe runs on a 2 second tray loop: a brain that accepts a connection and never answers used to park that thread and freeze the tooltip on its last value, which for a brain that came up and then hung is the green dot. `probe_health_at` uses a 1s connect / 2s total timeout and reports a silent brain differently from an absent one; `output_with_timeout` kills a probe that outlives `PROBE_TIMEOUT`.

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

**6. `feral-core` is not the only thing in CI any more, and the gap that remains is a different one.** A `Subprojects — pytest` matrix job runs `feral-nodes/python-node-sdk` and `feral-registry`; `feral-nodes/ts-node-sdk` has its own typecheck + vitest job. Still uncovered on the Python side: `feral-relay`, `scripts`, `sdk/python`, `packages`. The **Swift and Kotlin** SDKs are still never compiled.

    The live trap is narrower and easier to miss. The subproject job installs `pip install -e ".[dev]"`, and `python-node-sdk`'s dev extra is `pytest` + `pytest-timeout` only — **no `pytest-asyncio`**. An `@pytest.mark.asyncio` test therefore passes locally, where the repo `.venv` has the plugin, and does not run the coroutine in CI. Write async cases as sync tests calling `asyncio.run(...)`, as `tests/test_capability_grants.py` does and explains at its line 23. A test that runs in one place and silently no-ops in the other is worse than no test.

    On the JS/TS side CI covers `feral-client-v2` (build + vitest coverage + playwright e2e), `feral-extension` (vitest), and `feral-nodes/ts-node-sdk` (typecheck + build + vitest). The last two were added after an audit found they had committed test suites, lockfiles, and weekly Dependabot bump PRs, but no job that installed or ran them. The `client-build` job that built and tested v1 was deleted in 2026.8.12 along with `feral-client/` itself; if `client-build` is still listed as a required status check in branch protection, that setting has to be dropped by hand or every PR will block on a job that no longer runs.

`desktop/` is still uncovered: it is a Dependabot npm ecosystem whose only workflow is `workflow_dispatch:`, so a bump PR there builds nothing.

**7. `desktop/` is built by no automatic trigger.** `.github/workflows/desktop.yml` is `workflow_dispatch:` only — deliberately, per the note at the top of that file, because the artifacts are not shipped until signing certs land. A change to `desktop/src-tauri/` compiles in no CI run until somebody dispatches it by hand.

**8. `getattr(<MagicMock>, name, default)` NEVER reaches its default.** A mock has every attribute, so the default is the one place the author wrote down what the value should be and it is dead code under test. This shipped twice in one week. `/api/dashboard` answered 500 for a whole release on `float(getattr(state, "started_at", 0.0))`: `started_at` is a real `float` on a real `BrainState`, so the *name* was fine, and what broke production was the mock answering with a `MagicMock` where a float was declared, then `time.time() - <MagicMock>` raising `TypeError`. Separately, `_somatic_state_for_turn` returned a `MagicMock` that got JSON-serialised onto a chat response.

`tests/conftest.py` now closes this. Any spec-less mock assigned to the `state` attribute of an `api.*` module (`api.state`, `api.server`, `api.routes.*`) is wrapped in `_GuardedStateMock`, which raises `AttributeError` instead of auto-vivifying a child mock when the name is either absent from the real `BrainState` or holds a non-`None` `int/float/str/bool/bytes` there. `AttributeError` is the point: it is exactly what `getattr` catches, so the default goes live again and the trap closes rather than merely being reported. A bare `state.x` with no default fails on the spot instead.

Deliberately still allowed, because this is what mocks are legitimately for: attributes the test set itself (`mock.started_at = 123.0`, which Mock keeps in `__dict__`), object-valued collaborators (`orchestrator`, `memory`, `voice_router`), and `None`-valued fields, which boot fills in with objects later. The refused names are printed once at session end under `[state-mock guard]`.

Prefer the `brain_state_mock` fixture (`MagicMock(spec=BrainState)`) in new tests. `tests/test_state_mock_getattr_guard.py` is the proof the guard fires; 5 of its 11 tests fail with the original production symptoms if the wrapper is disabled.

## Conventions

- **`models/protocol.py` is canonical.** Never hard-code a wire constant that exists there. `HUP_VERSION` has already shipped a three-way mismatch; there is now an AST guard, but it only covers `api/server.py`.
- **Version literals are synced by `scripts/sync_versions.py`** across 14 file+regex locations and gated by `.github/workflows/version-coherence.yml`. Run `python scripts/sync_versions.py --check` after touching any version. (It was 17 until the three `feral-client/` entries went with the v1 client in 2026.8.12.)
- **Do not add a bare `except Exception: pass`.** There are already 1,702 broad handlers and ~210 silent swallows; they are the repo's dominant defect class. If you must catch broadly, log with context and re-raise or return a typed error.
- **Async discipline:** never call blocking I/O inside `async def`. Use `asyncio.to_thread` (85 existing call sites) or the async-native API. `subprocess.run` inside a route handler is a bug.
- **Background tasks must be referenced.** Use `state.register_background_task(...)` or hold the task in a `set[asyncio.Task]` with an `add_done_callback(the_set.discard)` so it stays bounded (see `agents/orchestrator.py:210-218`). A bare `asyncio.create_task(...)` **or `asyncio.ensure_future(...)`** can be garbage-collected mid-flight; the loop holds tasks only weakly. `tests/test_background_task_references.py` AST-scans for both and will fail the build on a new one.
- **No em dashes** in code, comments, commit messages, or docs.

## Where the truth is written down

- `CHANGELOG.md` (472KB) is a genuine forensic record — release notes include root-cause analysis. Grep it before assuming a bug is new.
- `AUDIT-r13/` and `AUDIT-r14/` are prior internal audits with `file:line` citations. `AUDIT-r14/round3/SCOREBOARD-r3.md` scores 21 areas; none reach 5/5.
- `feral-nodes/HUP_SPEC.md` is the device protocol spec, and it is **prose** — every SDK implements it by hand, which is why cross-language drift is a recurring bug class.
