# Extension, approval and connection integration recheck

October 2, 2026. Integrated core source
`a320f54bf1edb05901642463c98b70932bc39f63`, including timestamp/ACP/connector
commit `4e118360c`. Subsequent documentation commits do not change this core.
This is controlled local integration evidence. Exact-source remote CI remains
required; the earlier full backend result tested `dd69c7bf5`, not these changes.

## Final frozen-source checks

The pinned interpreter ran 28 backend suites in a disposable runtime home.
They cover registry preparation/replacement/cancellation, exact registry owner
fencing, redacted errors, explicit manifest approval precedence, central one-use
admission, plan mode/autonomy, app confirmations, runtime context, provider
failover, ACP scope, connector replacement and actual browser-wrapper binding.
Browser/controller and provider fixtures are controlled; no actual browser,
account, external delivery, purchase or physical device was involved.

- **1,003 passed, seven warnings in17.84s**, exit0. Targeted integration uses
  `--no-cov`; it is not another whole-backend coverage measurement.
- Full core Ruff passed the exact CI selection and existing ignored codes.
- Source manifest:1,264 Python files, digest
  `e41154bfa63cc5725c60e21622a6729e335e409f9071fb084c4742afadd3bb62`.
  Before/after manifests matched; changed files were empty.
- Python SDK recheck:112 passed/seven warnings in2.67s. Actual Node loopback
  host integration passed once with zero skips. These ran before the final narrow
  browser-wrapper follow-up; the final backend run includes that follow-up.
  No SDK implementation changed afterward. Exact-source CI must rerun its SDK steps.

Commands from `feral-core` use `FERAL_HOME`, `FERAL_DATA_HOME` and
`FERAL_AUDIT_LOG_PATH` beneath `/private/tmp/feral-review-final-home`:

```sh
../.venv/bin/python -m pytest \
  tests/test_registry_reload_recovery.py tests/test_skill_registry.py \
  tests/test_skill_hot_reload.py tests/test_skill_approval_reporting.py \
  tests/test_install_reports_hot_reload.py tests/test_marketplace_install_consent.py \
  tests/test_tool_runner_validator_invalidation.py \
  tests/test_skill_validator_matches_calls_not_words.py \
  tests/test_manifest_approval_precedence.py tests/test_tool_runner.py \
  tests/test_tool_runner_exact_approval.py tests/test_tool_runner_executor_admission.py \
  tests/security/test_safety_resolver.py tests/test_plan_mode.py \
  tests/test_earned_autonomy.py tests/test_app_action_dispatch.py \
  tests/test_apps_e2e.py tests/test_runtime_context_activation.py \
  tests/test_failover_endpoint_routing.py tests/test_acp_permission_context.py \
  tests/test_bridges_acp_client.py tests/test_native_integration_lifecycle_routes.py \
  tests/test_mcp_routes.py tests/test_channels.py tests/test_background_task_references.py \
  tests/test_browser_bridge_reload.py tests/test_browser_use_endpoints.py \
  tests/test_brain_browser_alias_map.py \
  -q --tb=short --no-cov -p no:randomly --timeout=60
../.venv/bin/python -m mypy . --cache-dir=/private/tmp/feral-review-final-mypy
../.venv/bin/python -m ruff check . --select=E,F,W --ignore=E501,E402,F401,W291,W293
```

## Type ratchet: improved, still failed

Full configured mypy reports **818 errors in234 files,1,264 checked**, exit1.
The baseline remains812; no baseline increase, added cast, Any annotation or
suppression was used. Comparison to the published `dd69c7bf5` Ubuntu log845
normalizes file, exact diagnostic/error code and multiplicity, ignoring line
shifts: **27 removed, zero added**. This is a comparison of actual diagnostic
sets, not a new Ubuntu execution result. The remaining type gate is not green.

An earlier integration run passed643 tests but measured819 errors. Its normalized
comparison caught a new `_BrowserSkillBridge` argument diagnostic introduced by
the strengthened instance-registration contract. The real wrapper now inherits
BaseSkill and preserves its controller/dispatch envelopes. The final1003-test
and818-error runs above follow that repair. Earlier logs/manifests remain intact.

Logs stay outside Git:
`/private/tmp/feral-review-final-integration-tests.log`,
`/private/tmp/feral-review-final-integration-mypy.log`,
`/private/tmp/feral-review-final-integration-manifest.json` and the preceding
`feral-review-integration-*` artifacts. SDK output is retained separately.

## Candidate and release boundary

The frozen9.30 app still contains `dd69c7bf5`; it does not contain this new source.
Its executable hash,482 packaged-source matches and strict signature were
rechecked unchanged. [Headless acceptance](NATIVE_9_30_ACCEPTANCE.md) passed on
that exact artifact. Current GUI acceptance is not run because Computer Use
cannot initialize. Managed interruption leaves context unready; explicit safe
recovery and same-thread continuation remain open. Signed distribution,
migration/upgrade, clean-machine install,
Linux and account/device acceptance remain open. No main merge or release occurred.
