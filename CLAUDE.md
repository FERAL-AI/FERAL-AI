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
| `desktop/` | Rust (Tauri) | Experimental shell. Ships no Python; spawns `python3 -m api.server` |

## Commands

```bash
make dev                                  # pip install -e "feral-core[llm,dev]" + client deps
make test                                 # cd feral-core && python -m pytest tests/ -v
make serve                                # feral serve
make doctor                               # feral doctor — reports real runtime state
```

CI equivalents (what actually gates a PR):

```bash
cd feral-core && ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .
cd feral-core && pip install --constraint requirements.lock -e ".[all,dev]"
cd feral-core && python -m pytest tests/ --cov --cov-fail-under=50
```

`make lint` does **not** lint — it runs pytest and prints a note. Use the ruff line above.

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
