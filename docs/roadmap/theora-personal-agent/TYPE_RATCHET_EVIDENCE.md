# Type ratchet triage: reviewed vault and bootstrap state

TYPE-01A, October 2, 2026. This bounded worker slice owns only
`security/vault_initialization_api.py`, `security/vault_coordinator.py` and
`security/agent_bootstrap_continuation.py` under `feral-core`. Parent integration
owns the full ratchet comparison, other diagnostics, final commit and CI update.

## Last Ubuntu evidence

The parent extracted the following diagnostics from
[CI run 37024016126, job 110893948351](https://github.com/FERAL-AI/FERAL-AI/actions/runs/37024016126/job/110893948351)
at source `6b368ccf79c8b581bf2f705ed23e97095899f49d`. The job reported **859 errors
versus the 812-error baseline**. These eight errors are attributable source gaps
in this slice, not a claim that all 47 extra errors share their cause.

| File and original lines | Diagnostic | Source contract |
|---|---|---|
| `vault_initialization_api.py`: 234, 240, 259, 272 | `object` passed where `VaultInitializationReview` is required (`arg-type`) | The stored tuple's first element is the frozen review returned by `review_initialization`; cancel and initialize accept that same type |
| `vault_coordinator.py`: 227, 275 | Assigning `AbstractEventLoop` to a value inferred as `None` (`assignment`) | Both assignments come from `asyncio.get_running_loop()`; initial absence is legitimate |
| `vault_coordinator.py`: 302 | Future result needs an annotation (`var-annotated`) | A worker authenticates an opaque object through the injected factory and publishes it through a concurrent Future |
| `agent_bootstrap_continuation.py`: 75 | Empty `_reviews` dictionary needs an annotation (`var-annotated`) | Entries are a monotonic float timestamp and an immutable `_Binding` |

These original line numbers precede the import/annotation edits. Earlier
unverified line numbers supplied during discussion were discarded; this table
uses the parent's actual log extraction.

## Narrow repair

- The initialization-review dictionary records `VaultInitializationReview` in
  its first tuple slot; the snapshot, timestamp and opaque initializer identity
  retain their existing roles.
- The owning loop is `asyncio.AbstractEventLoop | None`.
- The worker result is `Future[object]`, and the held authenticated vault is
  `object | None`. The injected contract allows opaque objects, so this does not
  assume every test/adapter is a concrete `BlindVault` implementation.
- The bootstrap review dictionary is `dict[str, tuple[float, _Binding]]`.

No control-flow, approvals, callbacks, timeout, token expiry, side effects or
release gates were changed. No new `Any`, cast, ignore or baseline increase was
introduced. Precise Future typing initially exposed the previously inferred
None-only held-vault field; its explicit optional-object annotation resolved
that local diagnostic without suppressing it.

## Local verification actually performed

From `feral-core`, using the repository's existing Python 3.11.15 environment
and **mypy 1.20.2** on macOS:

```sh
../.venv/bin/python -m mypy --follow-imports=silent --cache-dir /private/tmp/feral-type-vault-mypy-20261002 security/vault_initialization_api.py security/vault_coordinator.py security/agent_bootstrap_continuation.py
```

Result: **Success: no issues found in 3 source files**. The repository's
`mypy.ini` and Pydantic plugin remain active. `--follow-imports=silent` limits
reported diagnostics to this slice; this is not the full Ubuntu CI invocation.

```sh
FERAL_HOME=/private/tmp/feral-type-vault-home-20261002 FERAL_DATA_HOME=/private/tmp/feral-type-vault-data-20261002 ../.venv/bin/python -m pytest tests/test_vault_coordinator.py tests/test_vault_initialization_api.py tests/test_agent_bootstrap_continuation.py tests/test_agent_bootstrap_lifecycle.py tests/test_native_deferred_vault.py tests/test_native_health_readiness.py tests/test_native_vault_http_refusal.py -q --no-cov -p no:randomly
```

Result on final annotated source: **116 passed, 7 warnings in 1.97 seconds**.
These suites use fake OS/keyring operations, disposable encrypted storage and
existing test-home isolation. They cover readiness/refusal, single-flight
timeouts/cancellation, review expiry/reuse, fresh-vault encryption/readback,
preservation on failure and reviewed bootstrap fencing/recovery. They do not
exercise the user's Keychain or a signed production installation. Existing
warnings include model/lifespan deprecations and the conftest's report of restored
environment-variable changes within the test process.

From ASOS:

```sh
.venv/bin/python -m ruff check --select=E,F,W --ignore=E501,E402,F401,W291,W293 feral-core/security/vault_initialization_api.py feral-core/security/vault_coordinator.py feral-core/security/agent_bootstrap_continuation.py
git diff --check -- feral-core/security/vault_initialization_api.py feral-core/security/vault_coordinator.py feral-core/security/agent_bootstrap_continuation.py
```

Both checks passed. No SDK frozen source or `mypy-baseline.txt` was edited.

## Remaining gate

After integration/publication, rerun the same full-source Ubuntu/Python CI check
and attribute the remaining baseline differences by file and environment.
Do not subtract eight from 859 and call that the measured new count. Local
three-file success does not establish the full ratchet has passed. Record the
new source/run and actual diagnostics in [WORK_STATE.md](WORK_STATE.md).
