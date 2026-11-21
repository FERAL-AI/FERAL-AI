# Immutable native 9.29 acceptance

October 2, 2026. **The bounded crash, tracked-turn and lifecycle journey passed.**
No crash or unexplained exit was observed in these two isolated launches. This
does not certify the entire app or release. The original
[failed 9.28 report](NATIVE_9_28_ACCEPTANCE.md) and its probes remain
unchanged. Working-tree changes after assembly are excluded from this candidate.

## Exact candidate and isolation

- Native version 2026.9.29/build 2026100203, bundle ID `ai.feral.native.preview`.
- Source `634595c7194aedaf6eb9b8c91cbacb87fedea4bd`.
- Executable SHA256 `013ccc1250f6ed8c58bf80254ff332f93e0957226691f72c6c41f5c388f2f102`.
- App: `desktop-native/build/FERAL Native Preview.app`.
- Coordinator receipts: `/private/tmp/feral-candidate-9-29-manifest.json` and
  `/private/tmp/feral-native-9-29-bundle-audit.json`. Parent reports build/sign/
  strict signature verification exit 0; all 481 production Python files match the
  source; audit 13,060 files/265 Mach-O/9 internal links passed, with bundled
  Python 3.11.15/SQLite 3.53.1/FTS5/OpenCode 1.18.10. These are coordinator checks,
  separate from the actual interaction results below.
- Exact disposable root `/private/tmp/feral-native-populated-20261002`; isolated
  HOME, FERAL_HOME/DATA_HOME and prefs suite
  `ai.feral.native.acceptance.6d29504e06625a63`. No personal/default profile,
  keychain reset, account login, OS permission, messaging or purchase journey.
- First owned host 75269, exact backend child 75279, loopback port 57528. Launch
  observer exec session 65120. Relaunch host 79871, child 79897, port 58451,
  observer session 53067. Both deliberate quits returned exit 0 and both exact
  children were absent afterward. Identity was revalidated on both launches and
  final readback. Existing owned Ollama server on 11436 is reused;
  no model process/download was introduced. Selected existing alias
  `theora-coding-442789f441d80043`, Qwen2 family 3.1B/Q4_K_M, reports allocated
  context 16384. Core health reports 2026.9.7 separately from the native version.
- Disk declined from 5.4 to 3.3 and then 2.3 GiB free; after final quit the
  observed free space was 4.3 GiB. No cleanup was performed by this worker and
  the space change is not attributed to a particular cause. No disk exhaustion
  or disk-caused crash has been established. No large app copies were made;
  regular 9.29 evidence files total 851,544 bytes.

## Actual GUI and readback matrix

| Journey | Final result | Evidence and boundary |
|---|---|---|
| Previous 9.28 Security layout crash path | Passed bounded actual path | Vault→Permissions and cost→Cost caps→Permissions→Folders; fresh AX reads survived each navigation. Read-only, no tier/grant/cap changes. |
| Populated conversation preservation | Passed original rich and short baselines; fixture normalization recorded | Original rich 15 records/pin/renamed title and old short 10 records retain identical whole-message digests and summaries after final quit. Formatting fixture stays at 2 messages with every original content/role field retained, but live Chat saves generated row IDs and rederives its uncustomized title. Do not claim byte identity for that fixture. |
| Markdown/native code Copy and selection | Passed | Opened existing 9.28 synthetic formatting fixture, Continued into Chat, clicked Copy code, pasted exact `let acceptance928 = true` into unsubmitted composer, then cleared it. After relaunch, triple-click selected the code in the AppKit field; AX reports selected text, CmdC/paste returns the exact code. A stale CUA element index required a fresh AX read; the app stayed running. No copied text was sent. |
| Rich metadata accessibility | Passed bounded path | Rich original conversation metadata, live response details/usage, rich→New conversation all survive actual AX interaction. Historical false/pending action rows remain visible rather than being rewritten. |
| Three real tracked turns/current context | Passed | 13+29→42; remember marker CAFE929/reply Ready→Ready; ask the preceding marker→CAFE929. All three have matching durable terminal receipts and persisted user/assistant markers, `processing_outcome=completed`, `action_outcome=not_asserted`, no approval IDs. GUI reports `Request processing finished`. |
| Exact Stop | Passed | A separate harmless long-text request was confirmed running, then stopped through the native Stop button. Exact durable terminal outcome is cancelled/action unknown, with no final text; GUI keeps `Stopped; earlier actions may still need checking`. No effect success is asserted. |
| Native status reconciliation on relaunch | Passed automatic path | Another harmless long-text request was running at deliberate quit. Saved transcript marker was outcome_unknown while its durable receipt settled cancelled/action unknown. Startup automatically read the exact status and persisted its terminal marker without resubmitting the prompt. Manual Check status button was not clicked because automatic reconciliation had already cleared it. |
| Thread switch/New conversation/error recovery | Passed bounded path | Rich→new tracked chat, conversation preview→Continue, formatting→rich, and New conversation after restored cancellation/error all survive AX reads. Fresh connection disclosure says the exact new session does not inherit shared-primary history; earlier answers do not appear in it. |
| Quit/relaunch/child exit | Passed | Two deliberate CmdQ exits observed as 0; exact native child absent each time. Same-candidate relaunch preserves all three answers and reconciles the pending marker. Receipt count remains exactly 5: three completed and two cancelled, with no replay/new turn from restoration. |
| Actual Markdown link click / long Unicode wrap | Not run in this candidate | Prior component/fixture evidence is separate. Current screenshots have unusually large white capture margins; do not infer a layout failure from this capture representation alone. |
| Real effects/accounts/glasses/distribution | Not run | Outside the isolated read-only/model scope. No release, migration, physical-device, clean-machine or notarization certification. |

## Commands and evidence

Both launcher and observer require the exact full source/hash arguments; missing
or mismatching metadata/manifest/hash fails closed. The launcher `--check-only`
passed before actual launch. Actual launch used approved escalation and `--hold`
through the existing disposable-profile launcher contract.

```sh
python3 desktop-native/acceptance/native_9_29_launcher.py \
  --expected-source 634595c7194aedaf6eb9b8c91cbacb87fedea4bd \
  --expected-sha256 013ccc1250f6ed8c58bf80254ff332f93e0957226691f72c6c41f5c388f2f102
python3 desktop-native/acceptance/native_9_29_probe.py live \
  --expected-source 634595c7194aedaf6eb9b8c91cbacb87fedea4bd \
  --expected-sha256 013ccc1250f6ed8c58bf80254ff332f93e0957226691f72c6c41f5c388f2f102 \
  --output /private/tmp/feral-native-populated-20261002/9-29-initial-readback.json
```

New launcher/probe passed py_compile, scoped CI Ruff and 13 bounded adapter/guard
assertions before launch: exact identities, old-candidate rejection, read-only
check mode, scoped receipt query/redaction, UTF-8, no mutation, symlink rejection
and exclusive evidence output. These assertions are not GUI acceptance.

Actual screenshots from CUA, retained locally rather than published:

- `9-29-security-permissions.png`, `9-29-formatting.png` and
  `9-29-three-real-turns.png` under the disposable root.
- `9-29-selection-copy-ax.txt` and `9-29-new-after-recovery-ax.txt` preserve
  actual Copy and retained-error/New conversation accessibility evidence.
- `9-29-initial-readback.json` retains redacted message digests and exact live
  ownership, read-only SQLite/GET only. The observer never sends a prompt,
  WebSocket, abort, tool or approval request; Stop/status are actual native actions.
- Host log `native.log`; backend log `feral-home/native-desktop.log`.
  Preparatory archive mistakenly looked for the backend log at root level;
  original 9.28 explicitly copied logs are intact. Actual first and second
  backend logs were copied to `9-29-first-launch-backend.log` and
  `9-29-second-launch-backend.log` before any replacement. This is a probe path
  issue, not an app crash. Launch/exit receipts were also copied for each run.

## Exact correlated turns and context evidence

Every row below belongs only to synthetic session `thread-568a92a9-c`.

| Action | Request ID | Turn ID | Durable processing/action outcome |
|---|---|---|---|
| Number reply | `23e4e850-611b-46b7-87ec-2838450b0308` | `83aa7ae6-3d78-4090-acab-65758352a4c4` | completed / not_asserted |
| Ready reply | `425a2950-d968-4bb1-a076-d0c5cc537cff` | `f6ff2614-4392-4b0e-86a9-c76a1b93df02` | completed / not_asserted |
| CAFE929 reply | `264da998-bec4-4f72-aed6-e44abafc2e5e` | `0bfd3766-a6cc-44d2-adbb-41f8a31255c9` | completed / not_asserted |
| Native Stop | `34223cae-1b07-4f6f-bf0e-7e57395d5122` | `2f8f5055-f7c3-4b7e-8c68-14e0862ac3d9` | cancelled / unknown |
| Quit while pending, then status recovery | `cb81d670-d0e4-4494-81f2-bb6b55349ea4` | `42282ea8-a94e-4f1a-bff7-7e9fc34df2cf` | cancelled / unknown |

The actual backend log records main request byte totals narrowing from
32369→31972, 33642→31889 and 33743→31973 under the 32000-byte limit.
The three main context estimates were 12506, 12510 and 12492, with output reserve
1024/template reserve256/capacity16384. The logs explicitly say
`tokenizer_verified=false`; these are estimates, not tokenizer certification.
No context guard, guessed tool fallback or early four-minute timeout appeared in
these three completed turns. This does not establish arbitrary-length context.

Readback files include `9-29-tracked-third-pending.json` (the third turn was
already completed by readback despite its filename), `9-29-stop-before.json`,
`9-29-stop-after.json`, `9-29-quit-before.json`,
`9-29-first-quit-readback.json`, `9-29-relaunch-readback.json`,
`9-29-final-quit-readback.json` and `9-29-final-retention-comparison.json`.
The final comparison deliberately records the formatting fixture as changed;
inspection against the preserved 9.28 formatting readback confirms every
original field remains and only row IDs were added. Its title was not marked
custom; the explicitly renamed original rich title is preserved.

## Remaining gates

Actual manual status-button timeout/unknown and reconnect denial paths, long
Unicode/wrapping across window sizes, Markdown link clicking, live simultaneous
sessions, effectful allow/deny, voice, coding engines, other native destinations,
accounts, migration, physical glasses/iOS/Linux and distribution remain separate
acceptance. Prior unit and small AppKit probe results are in
[layout evidence](NATIVE_SELECTABLE_LAYOUT_EVIDENCE.md). Do not promote them to
full packaged-app acceptance. This bounded journey has ended; both verified
native hosts and their children are closed. The pre-existing model server was
left unchanged.

This report is frozen with the observed outcomes and remaining gates before
another candidate can replace the bundle.
