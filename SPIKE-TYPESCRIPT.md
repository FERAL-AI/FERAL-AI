# SPIKE-TYPESCRIPT

Deciding whether FERAL should be rewritten in TypeScript. This file is the
status board. Read `CLAUDE.md` first for the repo traps that will otherwise
corrupt your measurements.

**Nothing here authorises writing a TypeScript FERAL.** This is a spike: a set
of experiments whose job is to answer a question, then be deleted.

---

## The state of the argument

A 2026-08-11 audit of `d15645cd4` examined this and concluded **do not rewrite**.
Its reasoning, which every experiment below is designed to attack rather than
confirm:

| Claim | Status |
|---|---|
| The ML case for Python is thin. All reasoning is remote; `llm_provider.py` is 4,809 lines of hand-rolled httpx to 16 providers and imports no ML library | to re-verify |
| ~10,711 of 169,780 production lines sit in files importing any ML or numeric library | to re-verify |
| The duplication case for TypeScript is weaker than it looks. Exactly one HUP payload type reaches three languages, none reach four | to re-verify |
| **Distribution is where Python genuinely cost money.** Product correctness depends on how the user compiled CPython | the one finding nobody disputes |

That last row is the only real prize, and it is why this spike exists.

---

## Working agreement

Same shape as `AUDIT-FIXES.md`, which worked.

1. **Run it, do not reason about it.** Every claim about what a library can do
   must come from output you produced on this machine. Training knowledge about
   the JS ecosystem is stale by definition and this is exactly the question
   where that hurts.
2. **A negative result is a result.** "transformers.js cannot reproduce the
   vectors" is worth more than a confident guess in either direction. Say
   UNVERIFIED rather than asserting.
3. **Compare against the real alternative, not a strawman.** The rewrite is not
   competing with "Python as it is today". It is competing with "Python plus a
   bundled interpreter", which costs a fraction of 170k lines. Any experiment
   that only tests the Node side answers half a question.
4. **Kill the idea early if it deserves killing.** These are ordered so the
   cheapest experiment that could end the discussion runs first.
5. **Touch nothing.** Spikes live in the scratchpad, never in the repo. No
   commits, no dependency changes, no edits to `~/.feral`.
6. **Record the outcome here** with the command and its output.

---

## The decisive experiments

Ordered by how cheaply each could end the discussion.

### E-1 · Can Node load sqlite-vec?

**Status:** **WORKS, and it dismantles the argument it was meant to support.**

`npm install better-sqlite3 sqlite-vec` installs 4 packages in 2 seconds.
better-sqlite3 13.0.3 has **no install script at all** and ships 8 platform
prebuilds, proven by installing with `--ignore-scripts` on an empty cache and
running with `PATH=/usr/bin:/bin`: still loads, no `build/` directory ever
created. So Node does not trade "how you compiled Python" for "do you have a
C++ toolchain". That worry was unfounded.

**But then the premise collapsed.** sqlite-vec 0.1.9 builds no ANN index; it is
a brute-force scan, confirmed by perfectly linear scaling. Measured on this
machine, top-5 over 384-dim vectors:

| corpus | numpy | sqlite-vec vec0 |
|---|---|---|
| 12k | **0.46 ms** | 7.08 ms |
| 50k | **2.42 ms** | 28.53 ms |
| 100k | **3.97 ms** | 56.99 ms |

Both linear, numpy roughly an order of magnitude faster, and both return
identical top-5 rowids and distances to 7 decimals.

**So the "degraded" fallback is the faster path.** `embeddings.py` calls numpy
degraded at :8, :290 and :425, and tells users to rebuild CPython to escape it.
That advice is backwards on speed. The honest remaining argument for sqlite-vec
is memory: numpy holds the whole matrix resident (18 MB at 12k, ~154 MB at
100k) while sqlite-vec streams from disk. That is a real consideration at a
million chunks and irrelevant at twelve thousand.

One genuine inefficiency did surface: FERAL's full path measures 12.20 ms where
raw numpy is 2.26 ms, and the gap is Python `bytes` materialisation that the
function's own docstring already identifies. Cacheable. Not a language problem.

**Unverified:** sqlite-vec ships no musl prebuild; no Alpine machine here.

The strongest argument for leaving Python. `memory/embeddings.py:411-420`
records that a CPython without `--enable-loadable-sqlite-extensions` can never
load the extension, and `:288` calls the numpy fallback "the DEFAULT path for a
large share of installs". That fallback is active on this machine.

**Kills the rewrite if:** `better-sqlite3` needs a source build, so a Node FERAL
would swap "depends on how you compiled Python" for "depends on whether you have
a C++ toolchain". Same class of failure, new hat.

### E-2 · Are the vectors compatible?

**Status:** **WORKS WITH CAVEATS.** The stored vectors are reusable.

`@huggingface/transformers` 4.2.0 (the `@xenova` package is frozen at 2.17.2),
backed by onnxruntime-node, loads the official `BAAI/bge-small-en-v1.5`.

Cosine between Python fastembed and Node, same sentences:

* `pooling: 'cls'` -> min **0.99999893** across 5 sentences, and **0.99999487**
  across a 300-sentence near-duplicate corpus
* `pooling: 'mean'` -> 0.9312 to 0.9569, **incompatible**

CLS is the correct choice: fastembed's `_post_process_onnx_output` takes
`embeddings[:, 0]` then normalises. transformers.js defaults to
`pooling: 'none'`, so this has to be selected deliberately and **the wrong
choice degrades silently**, which is precisely the failure mode that hid a dead
semantic index for months in this codebase.

Querying Node embeddings against Python's existing stored vectors gives 98.3%
identical top-5 ordering, 99.9% overlap. A full Node re-embed scores no better.
So the 12,000 stored vectors would carry over: no re-embed on upgrade.

Throughput is a tie, 281.4 versus 281.5 sentences/sec batched.

Footprint is not: Node `node_modules` 407 MB (onnxruntime-node 210 MB,
transformers 141 MB, pulls in `sharp`) against ~101 MB for the Python side.

Can Node produce vectors interchangeable with the 12,000 already stored by
fastembed's `BAAI/bge-small-en-v1.5`? Embed identical sentences both sides and
compare cosine.

**Kills the rewrite if:** they are not near-identical. Every user would face a
full re-embed on upgrade, and this session already showed what that costs: a
dimension change silently disabled semantic recall for months.

### E-3 · What has no Node equivalent at all?

**Status:** running

`faster-whisper`, `openwakeword`, `mlx-lm`, Silero VAD through onnxruntime, and
`pty`/`fcntl`/`termios`/`resource` for the process supervisor. The audit claims
several have no Node story whatsoever.

**Kills the rewrite if:** a shipped capability would simply cease to exist.

### E-4 · Does bundling a Python interpreter win the same prize?

**Status:** **SOLVED.** It wins the prize, with zero source changes.

FERAL's own unmodified `sqlite_vec_available()`, same machine, only the
interpreter varies:

```
pyenv 3.11.11  (what ships today) -> False
bundled 3.11.13 (uv / pbs 20260807) -> True
```

Independently reproduced. The bundled build reaches `dlopen` for real (verified
with a bogus path, so it is wired rather than stubbed), `vec_version()` returns
`v0.1.9`, and indexed `MATCH ... k=3` KNN order matches a full scan. The pyenv
SQLite is *newer* than the bundled one, which isolates the cause to the CPython
compile flag alone.

Size: interpreter 58 MB on disk, 26 MiB published. With sqlite-vec, numpy and
aiosqlite, trimmed: 60 MB on disk, 20 MB gzipped, so about a 24 MB dmg against
today's 3.7 MB. That understates it though: `fastembed` became a base dependency
in 2026.8.3 and pulls 38 MB of wheels, onnxruntime alone being 18 MB. A
realistic bundle is 150 to 250 MB, dominated by onnxruntime. **A TypeScript
FERAL doing local embeddings ships `onnxruntime-node` and lands in the same
place**, so bundle size is not a differentiator between the two options.

Of the four broken releases the audit cites, bundling prevents two
(2026.4.11 tflite-runtime on 3.12, 2026.8.3 Pillow/fastembed on 3.14: both are
literally "the user's Python is version X"). It partly covers 2026.6.14, where
the credit belongs to locking, which this repo already has. It does nothing for
2026.8.2, where the TestPyPI canary silently skipped the publish. **That last
one is a pipeline defect a rewrite does not fix either, and `npm publish` has
the identical failure class, so it should be struck from the rewrite's
justification.**

Honest limits, recorded rather than glossed: bundling adds a channel rather than
removing one, so `pip install feral-ai` keeps every failure it has today and all
prevention is desktop-only. It introduces a 4-way build matrix where one pure
wheel covers everything now, and makes self-update the project's problem.

**UNVERIFIED and it could downgrade this verdict:** no codesigning or
notarization was tested. macOS hardened runtime with library validation can
refuse to `dlopen` a library not signed by the same team ID, which is exactly
the pattern `vec0.dylib` uses. Fixable with signed dylibs plus
`disable-library-validation`, but it must be proven on a real notarized build
before anyone relies on this.

One citation correction: the `include_router` regression is **2026.6.14**
(`CHANGELOG.md:751`), not 2026.6.13, which is a Gmail App Password release.

The control experiment. Ship a relocatable interpreter and the sqlite-vec
failure disappears without changing language.

**Kills the rewrite if:** it works. The prize is claimed at a fraction of the
cost, and the remaining arguments are preference rather than capability.

### E-5 · What does the rewrite actually cost?

**Status:** not started

Only worth measuring if E-1 through E-4 leave the idea alive. Not a line count:
`agents/orchestrator.py`, `memory/store.py` and `llm_provider.py` carry years of
behaviour encoded as comments explaining why each guard exists. This session
alone found seven defects that survived because nobody could see the intent. A
rewrite discards that context and re-earns it as new bugs.

---

## What would have to be true to proceed

Written down in advance so the answer cannot drift to fit the effort already
spent:

- Node loads sqlite-vec with **no** source build, on a clean machine
- Vectors are compatible, or the migration is one command and reversible
- Every capability in E-3 has a real Node path, not an adjacent one
- Bundling a Python interpreter **fails** to solve distribution
- The cost in E-5 is understood and accepted, including the lost context

If any of the first four fails, the honest answer is no, and the audit's four
systemic items (S-1 through S-4) deliver more value for less risk.

---

## Outcomes

Appended as experiments report. `SOLVED` / `PARTIAL` / `NOT SOLVED` / `UNVERIFIED`,
with the command and its real output.
