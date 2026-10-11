# Populated native candidate acceptance

October 2, 2026. Worker NATIVE-01A/03A. This records actual native app actions,
fixture checks and failures separately. **The candidate does not pass release
acceptance.** No signing, accounts, microphone, glasses, iOS or Linux claim follows
from this work. See [execution plan](EXECUTION_PLAN.md).

## Immutable candidate and isolation

- Inspected and launched the existing `desktop-native/build/FERAL Native Preview.app`:
  version **2026.9.26**, build **2026100117**, bundle `ai.feral.native.preview`.
- Executable SHA-256:
  `3dbfbbd63af7206bd99d81065e6f8a3f239bf0eb4bd3e31584f6c2cbdae7a0e3`.
- Bundled conversations route SHA-256:
  `663378f83c35d37f9c434f91fc04805953cf8bec82889c61b6aa929751fd996e`;
  bundled provider:
  `d78784cc52ceb0132e32c0738b2905c1462699a6dfef0a2a24b1e710d22b41dc`;
  bundled launcher:
  `c1f68f954dc13c0ddad600dae5ddce5ea6e6c6ccc7b0bcfb795c5ba9e1205c54`.
- Source checkout HEAD during fixture checks: `6b368ccf79c8b581bf2f705ed23e97095899f49d`.
  The older packaged candidate is identified by artifact hashes, not described as
  containing every later source commit. It was neither rebuilt nor restaged.
- Disposable root `/private/tmp/feral-native-populated-20261002`; distinct
  `HOME`, `TMPDIR`, `FERAL_HOME`, `FERAL_DATA_HOME`, random loopback `FERAL_PORT`
  and preference suite `ai.feral.native.acceptance.6d29504e06625a63`.
  No personal runtime/profile/credential store was selected or reset.
- 8.7 GiB free before checks. Reused installed Ollama and existing explicit test
  weight directory; no model download or app copy. Owned model server port 11436.
  No cloud fallback configured. Null keyring used for this disposable profile;
  this does not test signed encrypted key storage.
- Native onboarding actually selected Orb and `Acceptance Tester`. The service's
  `/health` reported agent-ready; vault stayed locked. All UI actions used CUA.

## Actual actions and observed outcomes

| Action | Evidence / result |
|---|---|
| Populated history | Seeded 55 two-message histories and one three-record rich thread using actual conversation API, plus the app's initial thread: 57 total. Rich seed readback matched exactly. |
| Pagination and search | Actual Conversations view loaded 25 then 50 then the remaining page; search returned the single rich title match. |
| Inspect / continue | Original user attachment, assistant reasoning/usage/custom field and structured tool record appeared as retained metadata; Continue reopened them in Chat. |
| Rename and pin | Native Rename saved `Acceptance rich renamed`; native Pin confirmed pinned. Actual backend readback showed `title_custom=true`, `pinned=true`. |
| Rich preservation during save | After actual native sends/autosaves, all three original records and every seeded field remained in API readback. The renamed custom title remained. |
| Cancel | Stop response ended the active native request UI, restored composer/attachment controls and retained the submitted user turn. This alone does not certify remote cancellation or absence of every possible late effect. |
| Attachment denial | Selected only the synthetic 74-byte text file. Native content-disclosure Cancel retained draft/chip and added no submitted attachment turn in the visible transcript. |
| Attachment allow | Reopened the exact review, chose Send with attachments. Actual transcript/API saved its upload ID, filename, size, content type and SHA-256. Model completion for this turn failed as described below; content understanding is not claimed. |
| Real local reply | Reviewed activation of already-installed `theora-coding-442789f441d80043` (a Qwen2.5 3B configuration with larger context). Native Chat produced visible **42** for `31 plus 11`; actual persisted transcript retained it. |
| Supervisor review | Cancelled Pause left Running. Reviewed Pause produced Paused and authoritative notice. Reviewed Resume restored Running. This does not prove every effectful entry point honors pause. |
| Actual core denial | A real model write proposal queued `coding_tools__write_file` for `acceptance-rich` and the exact disposable project path. Oversight showed the args/session/tier; Deny returned a denied receipt, queue emptied and the file remained absent. |
| Conversation switching | After restart, selected history 54 and continued it. Its two records appeared without rich-thread records; connection notice described separate-session context limitations. No concurrent active-session acceptance is claimed. |
| Relaunch and data | Orb/name, selected rich thread, 15 saved records, all 57 histories and renamed pinned metadata survived restart. Then normal quit/relaunch restored the selected history 54. |
| Normal quit / child cleanup | Observed host 55241 exit **0** after explicit Command-Q. Backend 55285 logged graceful shutdown; exact host/backend PIDs disappeared. |
| Unexpected exit / accessibility | Two crashes confirmed below. Backend lifeline still terminated the owned child; a crash is not a successful lifecycle result. |

## Release-blocking failures discovered

1. **Native accessibility recursion crash.** The first unexpected host exit at
   08:32:27 initially had no available crash report. The delayed report
   `feral-native-2026-10-02-083330.ips` subsequently identified host 48746,
   EXC_BAD_ACCESS / SIGSEGV, stack exhaustion with 67,390 frames and recursion
   depth 5,614 through SwiftUI `AccessibilityNode.accessibilityLabel`,
   `valueForAttribute` and AppKit accessibility entry points. A second monitored
   host, 57319, exited **-11** after opening the selectable Conversation connection
   disclosure in restored rich Chat and reading AX. This host runs macOS
   **27.0.1 (26A434)**. This is distinct from the older nil-preferences startup
   trap. The first report arrived late, so absence of an immediate `.ips` must
   not be interpreted as absence of a crash.
2. **Native visual state needs correction.** Opening connection information after
   rich-to-short switching produced a blank main body and clipped sidebar in
   actual CUA screenshots while AX initially still described Chat/history.
   Window zoom did not repair it. Fresh relaunch rendered the short thread
   normally. Further AX inspection of the restored rich thread reproduced the
   crash above. A screenshot/AX discrepancy is not a pass or proof of data loss;
   native layout and accessibility must be isolated and reverified.
3. **Plain installed Qwen3 activation is insufficient.** Qwen3 4B was reachable,
   but its runtime context was 4,096 tokens; Ollama logged input 6,891 truncated
   to 2,050. A 1,024-token response contained no visible answer, leading to a
   prompt-addition retry and prolonged Thinking. `/no_think` did not establish
   usable inference. Capacity-aware activation, prompt budget and thinking/output
   behavior need actual model-specific verification.
4. **Wrong fallback action after local input rejection.** The growing synthetic
   transcript exceeded the existing 32,000-byte local budget (33,031 bytes).
   Streaming fallback entered direct execution after another model failure and
   selected Weather although the request concerned a new file-write approval.
   It attempted `weather_current__current` with the complete synthetic prompt as
   the location argument. No successful weather/account operation is claimed.
   This unrequested routing needs fail-closed handling before effectful testing.
5. **Direct execution assumes a completed result.** The next bounded-input
   rejection fell back to `coding_tools__bash`; its pending approval result lacked
   `success`. Bundled `agents/direct_execution.py:101` indexed `result["success"]`,
   causing `KeyError: 'success'` in the background turn. A pending result must be
   handled as pending, with no automatic effect or fabricated outcome.
6. **Model falsely claimed completion while held.** The model wrote “I have
   written” despite the actual write remaining pending and the file absent. It
   also invented an approval link and wrong navigation destination. Native tool
   status separately said pending approval. Runtime outcome conditioning and
   authoritative review/result UX must prevent this contradiction. The denied
   file was not created. Approval and successful write were not tested after
   fallback failures; effectful journeys stopped for repair.

Raw local evidence remains under the disposable root (`launch.json`,
`native-exit.json`, `seed.json`, `ollama.log`, `feral-home/native-desktop.log`) and
the named OS crash reports. These runtime files, API keys, databases and full OS
reports must not be committed. The reproduction uses synthetic content only.

## Fixtures and reproducible commands

From `desktop-native`:

```sh
bash test_features.sh Conversation RichChat ChatTools Attachment SessionRecovery Oversight Security Operations RuntimeHealth
```

**Passed, exit 0:** all nine requested suites, automatically linked model's
20 groups, desktop's 39 assertions and error presentation's five assertions.
The first sandboxed compilation failed because Swift's macro server could not
apply its sandbox; the correctly escalated rerun passed. Swift 5 mode emitted
existing async-NSLock warnings in test code. These are source fixture checks,
not model/GUI/hardware evidence, and do not override the actual crashes above.

From ASOS, acceptance aids (requires macOS and authorization to run local apps):

```sh
python3 desktop-native/acceptance/populated_profile_probe.py \
  --root /private/tmp/feral-native-populated-20261002 \
  --app 'desktop-native/build/FERAL Native Preview.app' \
  --model-dir /path/to/already-installed/ollama-models
python3 desktop-native/acceptance/seed_conversations.py \
  --root /private/tmp/feral-native-populated-20261002
```

Inspect the supplied model directory first. Omit `--model-dir` on relaunch when
the owned model server is already running; add `--hold` to capture host exit
code. The launcher refuses an already-live recorded host PID and preserves
existing disposable settings. Seeding intentionally replaces its named synthetic
threads; do not reseed a profile whose acceptance changes are being inspected.
The seed aid verifies the exact owned backend/parent/port before mutation;
`--check-target-only` performs that verification without seeding.

Both Python aids passed `py_compile` and the CI-aligned targeted Ruff rules;
the launcher, seed operation, target verification and monitored relaunch modes
were actually run. No automated GUI certification is embedded in these aids.

## Remaining acceptance

Repair the reproduced accessibility/fallback/result/capacity defects, build a
separate candidate through the coordinated source freeze, then repeat this
populated profile matrix and the exact approved write, denial, cancellation,
pending-review restart, active-session-switch and partial-stream cases. Also
exercise snapshots/branch/app actions, grant revoke, paused dispatch, sleep/wake,
nonempty encrypted migration, real voice, clean installation/update and signed
distribution. Those gates remain open; this evidence is a defect-finding
acceptance slice, not a complete native release matrix.
