# Provider setup parity and asynchronous status integration

October 4, 2026. These corrections extend existing FERAL setup and testing;
they do not implement the new installer, subscription, voice or shopping adapters.
They are outside the immutable [9.36 app](NATIVE_9_36_ACCEPTANCE.md).

## Reproduced problems and resulting behavior

The backend catalog's registered models route returns string IDs. Native
settings and onboarding expected object rows, so a nonempty normal response
could not populate either picker. Both readers now accept string IDs and
homogeneous legacy object/id rows, including valid empty lists. Malformed,
mixed, oversized or foreign-provider payloads are refused. Discovery preserves
the selected/manual model; it does not silently activate another model or write
configuration. Existing review freshness and single-use writes remain unchanged.

A disposable run of real CLI `cmd_models_set` reproduced switching from OpenAI
to Ollama while retaining the old synthetic cloud endpoint and fallback.
The command now validates the supported provider, resets a changed provider's
endpoint to its catalog default and clears fallbacks, and reports the reset and
restart requirement. Same-provider changes preserve intentional endpoint and
fallback overrides. Unsupported providers or malformed settings refuse without
writing. Unrelated settings and model favorites remain. Real ConfigLoader reload
verifies the persisted result; the test clears prior runtime-exported environment
variables to represent the documented restart rather than a live hot-swap.

Separately, backend CI's managed-voice disconnect fixture assumed that leaving
the WebSocket context meant asynchronous cleanup had committed a terminal.
A controlled terminal-commit barrier now verifies the legitimate durable running
receipt, then exact cancelled/unknown status after actual SQLite commitment.
Request/turn identity, unavailable capability, checkpoint fencing, one model
request, no TTS and no replay assertions remain. This is a test timing correction,
not a change to production detach/cancellation behavior.

## Scope

- `desktop-native/NativeProvidersFeature.swift` and its focused test.
- `desktop-native/NativeOnboardingSetupFeature.swift` and its focused test.
- `feral-core/cli/model_commands.py` and new
  `feral-core/tests/test_models_cli_switch_contract.py`.
- `feral-core/tests/test_client_managed_chained_voice.py`.
- The parent also integrated the three-file
  [automatic learning correction](SELF_LEARNING_SWITCH_EVIDENCE.md).

## Executed verification

| Check | Result | Evidence limit |
|---|---|---|
| Native Providers feature compilation/runner | 21 fixture groups, 50 assertions passed | Controlled HTTP fixtures; no account or actual GUI |
| Native Onboarding feature compilation/runner | 52 assertions passed | Model DTO, empty/legacy/malformed/refusal, manual selection and retained reviews |
| Native production typecheck | Exit 0 | Actual production sources; no assembled new app |
| CLI targeted regressions plus existing picker/voice tests | 30 passed, 6 warnings | 12 new cases; disposable settings, no inference |
| Voice adapter plus abort suite | 55 passed, 6 warnings | Includes controlled commit race; overlaps parent total |
| Final combined parent backend, 31 suites | **695 passed, 235 warnings, 38.70 seconds** | Isolated profiles, unchanged source inventory, no microphone/account/device use |
| Core Ruff / scoped whitespace | Passed | Existing repository selections |
| Full nonincremental mypy | 809 errors in 233 files | Zero added/removed normalized diagnostics versus retained 809; not type-clean |
| Changed public Markdown local links | 259 links across 11 documents, none broken | Snapshot before this evidence/index addition; not external URL acceptance |
| Mintlify navigation | All 69 pages reachable | Does not test product features |

The final backend inventory contains 1,292 unchanged Python inputs; digest
`b3626d55b6e0e0b969b6dc81b18812ef16ebdd55bc8344dc09e31073e82fcf38`.
Focused counts overlap and are not summed. An existing CLI discovery test logged
a blocked OpenAI network attempt; no external inference occurred.

Private retained receipt names include
`feral-managed-disconnect-ci-receipt-20261004.md` and the
`feral-mac-setup-learning-integration-20261004-*` test/type/lint roots.
The full local documentation leakage script also sees three pre-existing ignored
local reference files and fails on their historical naming. Those files remain
excluded from staging/publication; the gate was not weakened. New changed public
documentation is separately reviewed for exposure and relative links.

## Next acceptance

Reassemble the exact reviewed source, inspect a genuinely nonempty installed
model catalog in both native surfaces and verify a committed first reply. Check
disabled learning produces no automatic discovery, then exercise exact Stop and
restart/status without replay. Preserve existing CLI/web default profiles and
test an explicitly selected shared profile; do not silently migrate user data.
Fresh-head CI is required. The new three-step installer and supported plan-login
flow remain [implementation cards](MULTITASKING_AND_EASY_SETUP_PLAN.md).
