# AUDIT-FIXES

Defects found in the 2026-08-11 audit of `d15645cd4` (v2026.8.8). Full report with evidence and methodology: https://claude.ai/code/artifact/7e82aa6b-86ce-4677-9af1-42f018e750bf

Read `CLAUDE.md` first — it documents traps that will otherwise corrupt your measurements.

---

## Working agreement

These findings were verified once, on one commit. **They are leads, not facts about your working tree.** The tree may have moved. Some may already be fixed. At least one first-pass claim in this audit was refuted only because a second agent re-ran the command.

For every item below, in this order:

1. **Re-verify.** Open the cited `file:line`. Confirm the defect is present as described. If it is not, record that in this file under the item and move on. Do not fix something you have not seen.
2. **Reproduce.** Make the failure observable — a script, a test, a log line, a `curl`. If you cannot make it fail, you do not understand it yet. Say so rather than guessing.
3. **Write the failing test first.** It must fail against current `main` for the stated reason. A test that passes before your fix proves nothing.
4. **Fix the cause, not the symptom.** If a broad `except` is hiding the error, the fix is usually both: narrow the handler *and* correct the call.
5. **Prove it.** The new test passes, `make test` still passes, and `cd feral-core && ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 .` is clean.
6. **Check for siblings.** Every defect here is a class, not an instance. Grep for the same shape elsewhere before closing. Signature changes in particular: grep every call site.
7. **Record the outcome** in this file — `FIXED` with the commit, `NOT REPRODUCIBLE` with what you found, or `DEFERRED` with why.

Hard rules:

- One item per commit. No drive-by changes.
- Do not refactor adjacent code because it looks wrong. Note it and move on.
- Do not trust a green test suite as evidence — see trap 3 in `CLAUDE.md`.
- If a fix requires changing a public signature or wire format, stop and ask. `models/protocol.py` is consumed by four other languages.

---

## P0 — do these first

### F-01 · Scheduled federated sync has never worked

**Status:** FIXED — see commit below.

**Re-verified 2026-08-11.** Present exactly as described. `SyncEngine.sync_with_peer`
is keyword-only after `peer_id` with no `passphrase`; `sync_scheduler.py:240` passed
one; `inspect.signature(...).bind(None, "peer-1", passphrase="x")` raises
`TypeError: got an unexpected keyword argument 'passphrase'`.

**Provenance correction.** The audit attributes the removal to `ba55caf4d`.
`git show ba55caf4d -- feral-core/memory/sync.py` shows no change to `passphrase`
or to `sync_with_peer` in that commit. The parameter's removal is not evidenced
there. This does not change the fix, but the citation should not be repeated as fact.

**Decision: dropped the kwarg, did not thread it through.** The engine already has
the better value. `_handshake_and_exchange` sends the module-level
`memory.sync.SYNC_PASSPHRASE`, which `ensure_sync_passphrase()` resolves at boot
(`api/state.py:1163`) as env, then vault, then freshly generated and persisted.
The scheduler's own `_passphrase()` read `os.environ` alone, so threading it would
have sent an empty passphrase on any install whose secret lives in the vault, which
is the normal case after v2026.5.38. Restoring the parameter would have created a
second, weaker source of truth for a value the handshake already reads.

`_passphrase()` was removed with its only caller, and replaced by a comment saying
why, so nobody threads it back in.

**Done-when, all three met:** the contract test binds the scheduler's call against
the real `SyncEngine` signature; `_StubEngine`'s signature is asserted against the
real one so it cannot drift again; and `_sync_one_peer` now catches `TypeError`
separately as `internal_error`, logged with a traceback, because sharing the
`exception` bucket with peer failures is precisely what hid this.

New tests fail 4/6 against the unfixed source and pass 6/6 after.

**Siblings found — each needs its own item, not fixed here.** `mypy
--ignore-missing-imports memory/` surfaces the same `call-arg` class elsewhere.
The worst is verified:

- `skills/impl/agentic_computer_use.py:272` calls
  `LLMProvider(provider=..., model=..., api_key=...)`, but `LLMProvider.__init__`
  takes `(self)` and accepts none of them. It sits inside `except Exception`, which
  logs a warning and returns `None`, so the VLM for computer-use has never
  initialised. Same shape as F-01: wrong kwargs, swallowed by a broad handler.
- `skills/registry.py:120` and `:329` — missing positional `skill_id` for `BaseSkill`.
- `mcp/registry.py:267` — missing named argument `url` for `MCPServerConfig`.

This is the strongest available argument for S-1: the type checker finds this class
mechanically, and it is the class that hid for 40 releases.

Original finding (superseded by the record above, kept for reference):

```
feral-core/memory/sync.py:1202            def sync_with_peer(self, peer_id, *, max_attempts, connect_timeout, handshake_timeout, backoff_base)
feral-core/memory/sync_scheduler.py:240   self.engine.sync_with_peer(peer_id, passphrase=_passphrase())
feral-core/tests/test_sync_scheduler.py:43  async def sync_with_peer(self, peer_id: str, passphrase: str = "")
```

---

### F-16 · The computer-use VLM has never initialised

**Status:** FIXED — commit below.

**Re-verified.** `LLMProvider.__init__` takes `(self)`; the call passed three kwargs.
Confirmed by `inspect.signature`, and mypy reported all three as `call-arg`.

**The audit's question answered:** there is no lying test double here. Nothing under
`tests/` so much as names `_get_vlm` — the path had zero coverage. Unlike F-01, this
hid through absence rather than through a drifted stub.

**Fix.** `LLMProvider()` then `await llm.switch_provider(provider, model=, api_key=)`,
which is the real configuration API. `switch_provider` is async, so `_get_vlm` is now
async; its single caller was already inside `async def _execute_task`.

`FERAL_VLM_PROVIDER` and `FERAL_VLM_MODEL` are documented knobs that were read into
locals and then discarded with the TypeError, so neither has ever taken effect. A test
now pins both reaching the provider.

**"Not configured" vs "failed to construct" are now separate.** The missing-key case
returns `None` silently and early, because the caller already renders an actionable
503 for it. Everything after that point runs with a key present, so a failure there is
never a configuration problem, and it logs at warning with `exc_info=True` saying so.
The old code collapsed both into one silent `None`, which is why a dead capability
presented as "Set OPENAI_API_KEY" to users who had already set it. That message is
the reason this was never reported as a bug.

New tests fail 5/6 against the unfixed source and pass 6/6 after.

**Siblings:** every other `LLMProvider(` construction in the tree is zero-arg. With
this fixed, the real `call-arg` count is **zero**; the three remaining are the false
positives documented in `mypy-baseline.txt`.

Original finding follows.

```
feral-core/skills/impl/agentic_computer_use.py:272   LLMProvider(provider=..., model=..., api_key=...)
feral-core/agents/llm_provider.py:494+13             def __init__(self):
```

Independently confirmed: `LLMProvider.__init__` takes `(self)` and accepts none of those three keyword arguments. The call raises `TypeError`, which the surrounding `except Exception` at `:273` catches, logs at warning level, and converts into `return None`. The caller treats `None` as "no VLM configured", so the feature degrades silently rather than failing.

This is the same shape as F-01 — wrong kwargs, swallowed by a broad handler — but the consequence is larger: an entire capability is dead, and the log line reads like a missing API key rather than a bug.

**Done when:** the call matches the real constructor, a test asserts the VLM actually initialises, and the handler distinguishes "not configured" from "failed to construct". Check whether `agentic_computer_use` has any passing test that would have caught this; if it does, the test double is lying the way `_StubEngine` was.

---

### F-02 · Input validation lives on the client, not the server

**Status:** open

```
feral-core/models/protocol.py:838-845                        brain — no constraints
feral-nodes/python-node-sdk/src/feral_node_sdk/schemas.py:151-172   SDK — full constraints
```

The SDK docstring claims it "mirrors the brain's `GlassesFramePayload`". It does not. The SDK enforces `device_id` length 1-128, `width`/`height` 1-8192, `sequence >= 0`, and a validator that base64-decodes `data_b64` against a 512 KiB cap. The brain declares all of them bare. `DeviceAnnouncePayload` has the same inversion (SDK bounds `rssi_dbm` to [-127, 20], brain does not).

Every constraint sits in the component an attacker controls.

**Verify:** read both class bodies side by side.

**Done when:** the brain's models carry the constraints, a test posts a hostile frame (empty `device_id`, negative `sequence`, oversized payload) directly at the brain and gets a typed rejection, and the SDK's claim of mirroring is either true or the docstring is corrected. Audit the other payload types in `protocol.py` for the same gap while you are there.

---

### F-03 · Frame size cap measures base64 characters, not decoded bytes

**Status:** open

```
feral-core/api/server.py:3672    if len(data_b64) > VIDEO_FRAME_MAX_BYTES:   # 512 * 1024
also: server.py:1823, :2304, :3283
```

`len(data_b64)` counts base64 characters while the docstring two lines above claims a decoded cap. Base64 inflates 4/3, so the effective ceiling is 384 KiB. A 400 KiB JPEG passes both SDK validators, reports a successful send, and is dropped by the brain with a log-only warning — HUP error 4020 is never returned.

**Done when:** the check measures decoded size, the caller receives an explicit protocol error rather than silence, and the docstring matches the code. Fix all four sites.

---

### F-04 · The default embedding path blocks the event loop

**Status:** open

```
feral-core/memory/embeddings.py:1219      return self._fastembed_embed(text)     # inside async def
feral-core/memory/embeddings.py:1221      return self._local_embed(text)         # inside async def
feral-core/memory/embeddings.py:1200-1202 batch variant, blocks once per text
feral-core/memory/embeddings.py:52        _detect_provider default is "auto"
```

`_embed_impl` is `async` and awaited from ordinary request paths, but both branches run synchronously on the loop thread. `_local_embed` ends in `SentenceTransformer.encode()`. `auto` resolves to exactly these two branches, so this is the default install.

Worse: `_ensure_local_model` is reachable from the same sync path and its docstring notes construction triggers a ~130 MB download. A cold first embed can stall the brain for the length of a model fetch.

**Done when:** both branches are offloaded via `asyncio.to_thread`, and a test asserts loop liveness during an embed — see the existing pattern in `feral-core/tests/perf/test_memory_latency.py`, which counts ticks of a 1 ms pulse coroutine rather than measuring wall clock.

---

### F-05 · Blocking `subprocess.run` inside async route handlers

**Status:** open

```
feral-core/api/routes/apps.py:379                git clone, timeout=120   ← worst
feral-core/skills/marketplace.py:245             git pull,  timeout=30
feral-core/api/routes/system_permissions.py:115  open,      timeout=3
```

The first can stall every concurrent coroutine for two minutes against a slow or hostile remote.

**Done when:** all three use `asyncio.create_subprocess_exec`. Then grep for the class: `subprocess.run`, `subprocess.Popen`, and `shutil.rmtree` reachable from any `async def`.

---

### F-06 · Unreferenced background tasks can be garbage-collected

**Status:** open

```
feral-core/api/routes/config.py:492, :510, :540   channel startup (Telegram / Slack / WhatsApp)
feral-core/api/state.py:790, :828, :1888          websocket broadcast
feral-core/memory/sync.py:1042 · agents/supervisor.py:452 · cost/loop_guard.py:275
agents/subagent_spawner.py:250 · services/mdns.py:351 · skills/impl/browser_use.py:254
voice/realtime_proxy.py:1668 · voice/gemini_realtime.py:843
```

`asyncio.create_task(...)` with no reference retained. CPython may collect a task held only by a weak reference. The config sites are user-facing: a channel that fails to start does so silently *and* can be collected mid-startup.

The repo already has the right pattern — `agents/orchestrator.py:210-218` and `memory/store.py:352-357` hold a `set[asyncio.Task]`, and `api/server.py` uses `state.register_background_task(...)`. These sites simply missed it.

**Done when:** every site uses the existing helper, and a ruff rule or test prevents reintroduction.

---

## P1 — after the P0 set

### F-07 · Gen-UI payload cap disagrees between host and brain by 6x

```
feral-core/genui/app_message_schema.py:113-119        json.dumps(v).encode("utf-8") → len
feral-client-v2/src/pages/AppSurface.types.ts:69-70   JSON.stringify(payload).length
```

Both use `MAX_PAYLOAD_BYTES = 64 * 1024`. Python's `json.dumps` defaults to `ensure_ascii=True`, emitting `\uXXXX` escapes — six ASCII bytes per BMP character, twelve per emoji. JavaScript counts UTF-16 code units.

Measured: `{"a": "中" * 11000}` is 66,009 bytes to Python (rejected) and 11,008 units to JavaScript (accepted). Any payload above ~10,900 non-ASCII characters passes the browser-side security guard and is refused by the brain.

**Done when:** both sides measure the same quantity, and a shared fixture of non-ASCII payloads is asserted in both test suites.

### F-08 · The two node SDKs write different key filenames for the same node

```
feral-nodes/python-node-sdk/src/feral_node_sdk/pairing.py:28   drops invalid chars
feral-nodes/ts-node-sdk/src/pairing.ts:17                      replaces them with "_"
```

Both write to `~/.feral/node-keys/<safe>.key`, so this is a live collision. `"sensor 01"` becomes `sensor01.key` in one and `sensor_01.key` in the other; a node paired through one SDK silently re-pairs under the other. Python's `str.isalnum()` is Unicode-aware and the TS class is ASCII-only, so `café` yields `café.key` vs `caf_.key`. Python's drop-based collapse is also many-to-one, so two node ids can share a key file.

**Done when:** one algorithm, specified in `HUP_SPEC.md`, with the same fixture table tested on both sides.

### F-09 · The install smoke test cannot fail, and runs after publishing

```
.github/workflows/install-smoke.yml    feral --help || true
                                       python -c "…" || true
                                       on: workflow_run: [Release], types: [completed]
```

Both verification commands end in `|| true`, so a wheel whose `feral` entry point raises on import passes. It installs `feral-ai` only, never an extra, while `scripts/install.sh` installs `feral-ai[all]`. And it runs *after* the release workflow, so a failure reports a bad release rather than preventing one.

Related and worth fixing together: `CHANGELOG.md:305` claims "CI now tests Python 3.14", but `ci.yml:82` is `['3.11','3.12']`. `requirements.lock` is a Python 3.11 artifact pinning `pillow==11.3.0`, so it structurally cannot catch the marker-dependent conflict that shipped `2026.8.3` broken.

**Done when:** the smoke job asserts rather than tolerates, installs `[all]`, and gates the publish instead of following it.

### F-10 through F-15 · Smaller confirmed items

| ID | Defect | Location |
|---|---|---|
| F-10 | `mlx-lm` and `sentence-transformers` imported at runtime, declared in neither `dependencies` nor any of the 33 extras | `agents/local_inference.py`, `memory/embeddings.py:1445` |
| F-11 | `pip install 'feral-ai[macos-extras]'` is printed to users; that extra does not exist, so pip installs nothing | `cli/main.py:2593` |
| F-12 | `[wake]` pulls `tflite-runtime`, which has no wheel for Python 3.12+. Nothing gates it | `pyproject.toml:308` |
| F-13 | Token budget uses `len(str(content)) // 4`, under-counting non-Latin and code-heavy content; can overflow provider limits | `agents/context_engine.py:197` |
| F-14 | Desktop updater configured with `"pubkey": ""` and `"signingIdentity": null` — no shipping channel exists | `desktop/src-tauri/tauri.conf.json` |
| F-15 | `FeralBrainClient.swift` exists in two directories and has diverged; `FeralSensorBridge.swift` is byte-identical in both | `feral-nodes/ios-bridge/` vs `ios-app/Sources/FeralBridge/` |

---

## Systemic work — after the defect list

Do these in order. Each makes the next cheaper.

**S-1 · Status: DONE (scoped) — commit below.**

`.github/workflows/ci.yml` gains a non-blocking `typecheck` job; `feral-core/mypy-baseline.txt`
is the committed baseline. **719 errors in 204 files** (mypy 1.20.2, Python 3.11).

*Citation check:* the audit says 324 errors in 103 files "at default settings". I measure
719/204 with `--ignore-missing-imports --exclude '^build/'`. Not necessarily a contradiction,
since the flags and scope differ, but 324 should not be quoted as the baseline. The reproducible
command is recorded in the baseline header.

**The exclusions are load-bearing.** Without `--exclude '^build/'` mypy does not run at all:
`Duplicate module named "agents"` / `errors prevented further checking`. Trap 1 in CLAUDE.md
degrades the tool to zero rather than to a wrong number.

**call-arg: 6 repo-wide, and only 3 are real.**

- Real, and they are F-16's: `skills/impl/agentic_computer_use.py:272` (`provider`, `model`,
  `api_key`). Fixed under F-16 rather than here, to keep one item per commit.
- False positive: `skills/registry.py:120` and `:329`. The call is `obj()` where `obj` is a
  `BaseSkill` *subclass*; mypy narrows to `type[BaseSkill]`, which does require `skill_id`,
  but every concrete subclass defines `__init__(self)` and passes it to `super()`. Verified
  in `pdf_reader.py:37`, `plan.py:101`, `code_interpreter.py:370`.
- False positive: `mcp/registry.py:267`. `url` has a default of `""`; the model constructs
  fine without it. Cause is that the pydantic mypy plugin **crashes** on mypy 1.20.2
  (`AttributeError: module 'mypy.expandtype' has no attribute 'ExpandTypeVisitor'`), so
  `Field()` defaults are not modelled. Plugin deliberately left off.

So S-1 fixed no call-arg errors directly: the only real ones belong to F-16. Saying so is more
useful than manufacturing three edits.

No `# type: ignore` was added for the false positives. CLAUDE.md records 52 existing unvalidated
suppressions; a false positive documented in the baseline is auditable, one silenced in source
is not.

Once F-16 lands, call-arg is at zero, which makes it the cheapest code to promote from warning
to failure, and it is the class that hid F-01.

Original plan text follows.

**S-1 · Turn on the type checker.** Add `mypy --ignore-missing-imports` to `ci.yml` as a **non-blocking** job, then ratchet down. Baseline is 324 errors in 103 files at default settings — reproduce it before changing anything. 111 are the `AttributeError` class (`attr-defined` + `union-attr`); 6 are `call-arg`, the class that produced F-01. Annotations already cover 92.5% of parameters, so this is configuration, not annotation work. Note the 52 existing `# type: ignore[code]` comments in source are unvalidated suppressions from an earlier unrecorded mypy run.

**S-2 · Widen the lint.** Add `BLE001` (1,452 hits), `S110` (210), `RUF013` (56), `ASYNC240` (37), `B023` (4), `B006` (1) to the ruff selection. Introduce them one rule at a time with a per-rule baseline so the build stays green. `ASYNC240` in particular would have caught F-05. Also extend lint beyond `feral-core` — six other Python trees (102 files) have none.

**S-3 · Generate the protocol instead of hand-writing it.** Export `pydantic.model_json_schema()` from `models/protocol.py` and generate the TypeScript, Swift, and Kotlin types from it. This eliminates the F-07 and F-08 class permanently and is a prerequisite for any TypeScript work. Today `HUP_SPEC.md` is prose and every SDK is a human reading it; `HUP_VERSION` is hand-written in six places across four languages, and `tests/test_hup_version_unified.py` keeps them aligned by regex-scraping TypeScript and Swift source from Python.

**S-4 · Fix distribution without leaving Python.** Ship a relocatable interpreter (`python-build-standalone` or `uv`'s managed Python) inside the Tauri bundle. This permanently removes the worst finding in the audit: `memory/embeddings.py:411-420` documents that an interpreter built without `--enable-loadable-sqlite-extensions` (pyenv's macOS default) makes `sqlite-vec` unloadable, and `embeddings.py:288` calls the resulting brute-force fallback "the DEFAULT path for a large share of installs". Product correctness currently depends on how the user compiled CPython.

---

## Known-clean — do not spend time here

Verified during the audit and found sound. Listed so nobody re-investigates.

- **The GIL is not a problem.** Zero `ThreadPoolExecutor`, `ProcessPoolExecutor`, `multiprocessing`, or `concurrent.futures` anywhere, tests included. The only two GIL references *rely* on it, for atomic deque operations at `perception/glasses_buffer.py:123`.
- **All 31 `asyncio.run()` call sites are safe.** `security/content_defense.py:601` correctly probes for a running loop and degrades.
- **No secrets are committed.** `.env` is gitignored and absent from `git log --all`; a history scan for key formats finds only test fixtures. (The untracked working-copy `.env` does hold live credentials and sits at the repo root — one `git add -f` from exposure.)
- **Dependency management is genuinely strong.** Committed lockfile with 135 pins, explicit upper bounds with written rationale, an unconstrained smoke matrix, a TestPyPI canary. Do not "improve" it without reading the inline comments in `pyproject.toml` explaining why each bound exists.
- **`genui/a2ui_protocol.py` has zero importers.** It is dead code and contradicts `genui/generator.py` on component names. Delete rather than reconcile.
