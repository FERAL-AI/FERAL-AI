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

**Status:** running

The strongest argument for leaving Python. `memory/embeddings.py:411-420`
records that a CPython without `--enable-loadable-sqlite-extensions` can never
load the extension, and `:288` calls the numpy fallback "the DEFAULT path for a
large share of installs". That fallback is active on this machine.

**Kills the rewrite if:** `better-sqlite3` needs a source build, so a Node FERAL
would swap "depends on how you compiled Python" for "depends on whether you have
a C++ toolchain". Same class of failure, new hat.

### E-2 · Are the vectors compatible?

**Status:** running

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

**Status:** running

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
