# Direct-mode fallback contract

October 2, 2026. Parent owns `feral-core/agents/direct_execution.py` and
`tests/test_direct_execution_contract.py`. This repairs a defect observed in the
immutable 9.26 candidate; actual new-candidate verification remains required.

## Reproduced problem

The [populated native acceptance](NATIVE_POPULATED_ACCEPTANCE.md) exceeded the
old local input guard. After model failure, direct execution selected the first
entry in an expanded core tool bag: Weather for a file-write request. A later
fallback selected the first coding endpoint, bash, and indexed `success` in a
pending-approval envelope, causing a background KeyError. Neither tool ordering
nor semantic retrieval establishes the user's intended operation.

## Source repair

- Direct mode accepts a literal trigger at the start of the command, with a
  word boundary and optional `please`. It prefers the longest match; ties
  across different skills refuse. A greeting with a task appended is no longer
  swallowed as an ordinary greeting.
- A generic skill must have one unambiguous endpoint and pass the existing
  strict manifest-aware read-only classifier. Untrusted declarations, required
  approvals, multiple operations and missing required arguments refuse before
  dispatch. Computer/daemon actions require a working model and exact review.
- Dispatch uses the existing ToolRunner, preserving trusted session identity,
  plan/policy/lease checks, validation and result handling. It never calls the
  raw executor as a fallback. The old memory/daemon helpers remain for
  compatibility, but this automatic fallback does not enter their bypass paths.
- A pending result reports that the action was sent for review, with the card
  as current authority. It does not claim execution or assume approval remains
  pending if the user responded during notification. Invalid/error envelopes
  never render completion. Successful false/zero data are not treated as
  missing solely because they are falsey.

This intentionally narrows disconnected-model natural-language execution.
Existing normal model/tool and explicit review flows remain available. No new
dispatcher, safety bypass, model download or automatic effect is introduced.

## Local checks

From ASOS, using the existing environment and disposable runtime storage:

```sh
FERAL_HOME=/private/tmp/feral-coding-integration-20261002/home FERAL_DATA_HOME=/private/tmp/feral-coding-integration-20261002/data .venv/bin/python -m pytest feral-core/tests/test_direct_execution_contract.py feral-core/tests/test_tool_runner_call_context.py -q --no-cov -p no:randomly
```

**26 passed, 6 warnings in 0.86 seconds.** The new contract has 16 cases,
including the wrong-tool-bag reproduction, ambiguous triggers/operations,
trusted context through a real ToolRunner, pending/denied/malformed envelopes,
missing arguments/runner, untrusted metadata, daemon refusal and plan controls.
The executor is a recording fake; these checks do not perform network searches
or computer actions. Existing lifespan/model/environment warnings remain.

The first new-fixture run failed three cases: the synthetic Weather ID was not
a trusted built-in manifest, and manifest defaults require strings rather than
native booleans/integers. Fixtures were corrected to a real first-party tool ID
and declared default shape; no classifier or assertion was weakened. One edit
command initially used a path relative to the wrong directory and made no
change; the corrected edit and final checks passed.

Targeted Ruff and whitespace checks passed. Worker ToolRunner hardening
was concurrent during this targeted run; final frozen-source integration is a
separate gate. Rebuild/stage the exact combined source and repeat the original
native model-failure/pending-review journeys before calling this accepted.
