# Native 9.31 candidate acceptance

October 2, 2026. This report applies only to the identified candidate. Source
tests, actual packaged-backend execution, GUI interaction and distribution
acceptance have separate results.

## Immutable candidate

| Field | Verified value |
|---|---|
| App | `desktop-native/build/FERAL Native Preview.app` |
| Preserved immutable copy | `/private/tmp/feral-candidate-9-31-preserved.app` |
| Version / build | `2026.9.31` / `2026100205` |
| Bundle identifier | `ai.feral.native.preview` |
| Production source | `2d6839ab49cc570959cc3b9057181b0b6c3eba3e` |
| Executable SHA-256 | `ccf2fa65bec6a0caf4bd88c9cd69515533ec82e91908006f11c808502b31fedb` |
| Packaged source comparison | All 484 production Python files equal the source commit |
| Bundle audit | 13,095 files; 265 Mach-O objects; 9 internal symlinks; no audit issues |
| Bundled runtime | Python 3.11.15; SQLite 3.53.1 with FTS5; OpenCode 1.18.10 |
| Signature | Strict deep ad-hoc verification passed; not Developer-ID/notarized |

The previous 9.30 artifact was preserved before assembly. Its acceptance results
do not automatically apply to 9.31. The candidate manifest is
`/private/tmp/feral-candidate-9-31-manifest.json`; audit/source receipts are
`/private/tmp/feral-native-9-31-bundle-audit.json` and
`/private/tmp/feral-native-9-31-source-comparison.json`.

## Source checks

The final frozen native checks passed production typecheck, selected recovery,
vault, onboarding, turn, conversation, chat-tools and session suites, including
26 linked-model groups, 39 desktop assertions and 5 error-presentation assertions.
All 87 production/test Swift inputs were unchanged throughout verification.
The durable recovery journal has actual filesystem refusal and restart fixtures.
See [completion evidence](COMPLETION_WAVE_EVIDENCE.md) for the input digest,
commands and boundaries, and [backend recovery evidence](SAVED_CONTEXT_RECOVERY_EVIDENCE.md)
for exact-session, stale-generation, active-writer and approval contracts.

## Actual packaged-backend recovery attempts

The acceptance script uses the candidate's bundled Python and core, authenticated
loopback HTTP/WebSocket transport, private fresh synthetic storage and the
existing local model. It does not operate the native GUI or a personal profile.
It refuses fallback providers, tool dispatch, task replay and reuse/reset of an
existing attempt directory. A completed journey must observe actual IN_PROGRESS,
exact cancellation, foreign/stale recovery refusal, explicit recovery, independent
read-only status/capability checks, unchanged UI projection, a real recalled fact
and restart continuity. All owned processes must stop.

### Attempt 1: preserved failure

Result: **failed/incomplete**, not a recovery pass. The packaged backend started
and selected the configured existing Ollama model without fallback. During the
first synthetic fact request, the model invoked the read-only
`notes_memory__list_conversations` tool despite a text-only prompt. The test
aborted on that actual tool frame before reaching Stop/recovery/restart.
Prompt instructions were insufficient to establish a tool-free test.

The owned process terminated on SIGTERM, exit -15, without forced kill; its PID
and listener were independently absent. The candidate was not changed. Evidence
remains in `/private/tmp/feral-native-9-31-recovery-20261002/final-result.json`
and the adjacent synthetic server log. This attempt must never be reset or
silently replaced by a later successful attempt.

### Attempt 2: preserved fixture-setup failure

Result: **failed/incomplete before any model task**. All 43 installed empty
manifests loaded, but the boot browser fallback replaced the empty browser
manifest with 49 browser endpoints. The authenticated zero-inventory assertion
refused to send any task. The owned process stopped on SIGTERM, exit -15,
without forced kill; PID and listener were absent. Evidence remains in
`/private/tmp/feral-native-9-31-recovery-20261002-attempt-2`.

### Attempt 3: actual bounded recovery passed

Result: **passed**, exit 0. The supported authenticated skill-reload route loaded
the installed empty browser manifest after boot. Exact zero-tool and empty
endpoint readbacks passed before any task and again after restart. All 43 skill
identities match the candidate-derived fixture. This is explicit disposable
fixture configuration, not a production patch or global tools-disabled setting.

Verified actual outcomes:

- Real existing local-model reply acknowledged a synthetic fact and committed
  context. An actual IN_PROGRESS turn was observed and cancelled through its
  exact tracked request/turn identity; context remained unready.
- Another selected session and a stale revision could not recover it; refusals
  left the checkpoint unchanged. Reviewed recovery rotated generation/revision
  authority and retained uncertain prior action effects.
- Exact old-fence read-only recovery status and an independent capability
  readback confirmed the durable READY checkpoint. Status did not mutate it.
- The original UI transcript projection stayed unchanged. The cancelled input
  was absent from restored context. A real new model turn recalled the committed
  fact, then an owned backend restart preserved the exact checkpoint before a
  further model turn recalled it again.
- Four durable terminal receipts exist, exactly one for the cancelled request;
  zero tool executions were recorded. No cancelled task was replayed.
- Both owned servers terminated on SIGTERM, exit -15, without forced kill;
  their PIDs and listeners were independently absent.

Evidence directory:
`/private/tmp/feral-native-9-31-recovery-20261002-attempt-3`.
The final result, candidate/fixture hashes, pre/post inventory, exact reload
acknowledgements, hashed response receipts and shutdown evidence are retained
separately from attempts 1 and 2. No personal profile or external account was used.
The model was the already present Ollama alias at loopback port 11436, with no
fallback provider or model download.

Reproduction requires a new canonical attempt suffix, an unchanged identified
candidate and its candidate manifest; existing evidence roots are never reused:

```sh
.venv/bin/python -B desktop-native/acceptance/saved_context_recovery_probe.py \
  --candidate-version 31 \
  --candidate-app /private/tmp/feral-candidate-9-31-preserved.app \
  --attempt-root /private/tmp/feral-native-9-31-recovery-20261002-attempt-new \
  --expected-source 2d6839ab49cc570959cc3b9057181b0b6c3eba3e \
  --expected-sha256 ccf2fa65bec6a0caf4bd88c9cd69515533ec82e91908006f11c808502b31fedb
```

The external acceptance harness includes the fixture correction after candidate
assembly. Its SHA-256 during this run was
`b8721643b96d806b1262eb44131b3d3b7dd47f1372347fee1dc25c40ceffa1b0`.
Post-run strict signature, executable hash and all 484 packaged production-source
comparisons passed unchanged. This controlled text-context acceptance cannot
certify native GUI behavior, tool-enabled tasks, voice or general agent quality.

## Open acceptance gates

- Actual 9.31 native GUI journey: not run. Computer Use inventory still fails
  with `Sky Computer Use native pipe startup failed`; this is a testing-tool
  failure, not a newly observed FERAL crash.
- Fresh cloud setup: source/fixture checks pass, but the ad-hoc artifact correctly
  refuses fresh initialization. A verified Developer-ID build with a genuine
  version-bound OS acceptance receipt and authorized provider testing is needed.
- Voice/audio, populated-profile migration, clean-machine installation,
  upgrade/rollback, sleep/wake, Intel/Linux and physical-device/account journeys
  retain their individual acceptance gates.
- No real messages, purchases, account login, Keychain reset, merge or release
  occurred. The app remains a candidate until its enabled journeys pass.

Gen-UI expansion is deferred. Existing coding integrations are preserved; new
coding-engine expansion follows dependable setup, recovery, voice and data
continuity.
