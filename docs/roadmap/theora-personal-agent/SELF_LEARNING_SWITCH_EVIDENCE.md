# Automatic skill discovery: settings and cost authority

Reconciled October 4, 2026. This correction follows an actual 9.36 observation:
automatic skill discovery ran after a completed chat even though self-learning
was disabled. The candidate remains unchanged; source verification below does
not imply packaged acceptance.

## Behavior

`api/server.py` now uses the existing learner's authoritative
`FERAL_SELF_LEARNING` setting before capturing history or scheduling automatic
discovery. It checks the live setting again before detection, generation and
delivery. Disabling learning during an in-flight model request prevents later
work and proposal delivery; it cannot undo already incurred inference cost.

Automatic detection and generation use the existing `learner` cost call site.
Explicitly requested generation retains its previous call signature and `chat`
budget, still producing a proposal requiring approval. The independent proactive
loop switch is not treated as the learning switch. Undelivered generated content
is discarded only if the pending map still holds the exact object owned by that
attempt; another draft sharing its identifier is not removed.

Files: `feral-core/api/server.py`, `feral-core/agents/skill_generator.py`, and
`feral-core/tests/test_chat_turn_failure_wire.py`.

## Executed verification

- Parent integration: **664 passed, 242 warnings**, 28 suites, 38.39 seconds of
  pytest time. All 1,291 Python inputs stayed unchanged across the run.
- The suite includes foreground terminal/failure/abort/receipt/checkpoint tests,
  managed voice, provider/output configuration, learner and retained background
  task lifecycle coverage. The focused worker wave passed 26; it overlaps the
  parent total and is not added to it.
- Actual `LLMProvider` over a mock HTTP transport and actual SQLite cost ledger
  verify automatic limits 200/1500 billed to `learner`, refusal before HTTP when
  that budget is exhausted, and explicit generation billed to `chat`.
- Revocation tests cover the terminal, detection and generation boundaries;
  disabled discovery allocates neither captured history nor a retained task.
- Core Ruff and whitespace checks pass. Full nonincremental mypy still reports
  **809 errors in 233 files**; normalized diagnostics have zero additions or
  removals versus the retained preceding 809-error run. This is not type-clean.

Frozen Python inventory digest:
`3b6d2e58246233c4d7bcd04789676a2bcfb1a93854be90eb910fb3177856094e`.
Private integration receipt roots identify the tests, typing and lint runs;
they are not shipped archives or personal runtime data.

The later frozen setup/status integration includes this correction and passes
695 checks across31 suites with1,292 unchanged inputs. Counts overlap; the
664 result is the preceding focused parent wave. See
[combined evidence](SETUP_PARITY_EVIDENCE.md).

## Remaining acceptance

Reassemble the reviewed source and verify in the actual packaged app that a
disabled learning switch produces no automatic learner calls, and that enabling
it observes the selected budget and approval policy. Do not attribute the
candidate's 70-second foreground latency to this post-terminal discovery.

Concurrent proposal approval still has a separate identifier-collision gap.
Immutable proposal identity, trusted proposer/session/turn ownership and
one-use exact-content review are required before claiming cross-session skill
proposal isolation. This correction neither installs generated content nor
closes that broader contract.
