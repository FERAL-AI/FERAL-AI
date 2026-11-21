# Native setup, browser and voice contract hardening

October 2, 2026. This follow-up corrects reproduced source-contract defects after
the 9.31 candidate was frozen. These changes do not alter that preserved artifact.

## Corrected behavior

| Area | Reproduced failure | Correction and boundary |
|---|---|---|
| Optional vault setup | Current PR backend CI's GET sweep received 503 from an absent optional initializer | Passive status now returns 200 with `initializer_not_configured`, no preparation authority and explicit uninspected storage. Review, initialize and cancel mutations retain 503 refusal. No key/artifact inspection is performed. |
| Installed browser override | A valid installed empty manifest was replaced at startup with 49 fallback endpoints | Preserve any existing manifest, including an intentional empty inventory. Missing manifests still receive the existing fallback. No undeclared endpoint acquires dispatch authority. |
| Native voice configuration | Media could play while configuration awaited ACK; missing/malformed session IDs and a wrong provider ACK were accepted | Capture requires exact session and requested provider ACK; audio/captions/speaking state require active capture. Pre-ACK fallback/degradation status remains supported. |
| Speech settings | A foreign response URL could establish configuration/readiness | HTTP response URL must match the original request. Existing redirect refusal remains intact. |

The browser regression uses an actual installed empty package, real
`SkillRegistry`, startup registration and `ToolDispatchValidator`. It first failed
with the old source, then passed with preserved manifest identity/bytes/registry
generation, no exposed browser endpoints and refusal of undeclared `get_tabs`.
The optional status regression covers absent configuration and disabled release
acceptance, asserting no storage/Keychain calls and unchanged mutation refusals.

The native parser separately accepts the unavailable optional status and cannot
mint or dispatch an initialization review from it. Voice fixtures exercise PCM
conversion and simulated I/O, not audio devices. Prior-source assertion failures
were test executable traps, not observed app crashes.

## Final local verification

- Combined backend: **386 passed, 6 warnings in 9.69 seconds**, exit 0. Includes
  the unchanged actual subprocess boot/authenticated GET-route sweep, vault
  initializer API, browser reload/endpoints and browser alias-map suites.
- Selected native runner: **37 voice, 45 speech-configuration and 33 vault-setup
  assertions passed**, plus **26 linked-model groups**, 39 desktop assertions and
  5 error-presentation assertions. Production native typecheck passed.
- Whole-core configured Ruff passed. Fresh-cache whole-core mypy: **811
  diagnostics in 233 files, 1,269 checked**, matching all prior final messages
  exactly: zero added and zero removed. The tracked count baseline remains 812;
  existing diagnostics are not erased by this aggregate pass.
- Mypy input manifest: all 1,273 files unchanged before/after, aggregate SHA-256
  `ca24d02b9852ded12dbd9e0f1c88ccc6bc9580802eaaa5cc7cbfc044f6aabf8d`.
  Differences from the prior 811 snapshot are exactly the four owned backend
  implementation/test files. No configuration or baseline was changed.

Commands, from the named subdirectories:

```sh
# feral-core; synthetic FERAL_HOME and FERAL_DATA_HOME
../.venv/bin/python -m pytest tests/test_vault_initialization_api.py \
  tests/test_every_route_answers.py tests/test_browser_bridge_reload.py \
  tests/test_browser_use_endpoints.py tests/test_brain_browser_alias_map.py \
  -q --tb=short --no-cov -p no:randomly --timeout=60

# desktop-native
bash test_features.sh Voice VoiceConfiguration VaultSetup
bash build.sh --typecheck
```

Local records: `/private/tmp/feral-hardening-backend-final-20261002.log`,
`/private/tmp/feral-native-hardening-final-tests-approved-20261002.log`,
`/private/tmp/feral-native-hardening-final-typecheck-approved-20261002.log` and
`/private/tmp/feral-browser-router-final-mypy-20261002` log/input manifests.
The first sandboxed native invocations failed because Xcode's macro plug-in
could not start (`sandbox_apply: Operation not permitted`). Authorized reruns
passed; the failed logs are retained separately.

## Remaining voice implementation

These bounded native fixes do not establish complete voice/task multitasking:

1. Client chained Start needs to open the actual pipeline before success ACK;
   chained output currently uses `audio_chunk`, outside native playback types.
2. A voice attempt and response identity must follow provider callbacks so old
   same-session media cannot attach to a replacement start.
3. Chained speech interruption currently can cancel the agent command child;
   response interruption and explicit task abort need separate semantics.
4. Managed saved-context voice stays guarded until completed utterances use the
   existing exact-task command/checkpoint/authorization lifecycle. Removing that
   guard alone would create concurrent unowned writers. Realtime callback
   mutation needs its own owned-turn integration before it is advertised.

These are source-confirmed next contracts, not completed features. Actual
microphone/TCC, provider streaming, signed setup, GUI, migration, installation,
device and account acceptance remain separate gates. Gen-UI expansion is
deferred; existing coding integration is preserved.
