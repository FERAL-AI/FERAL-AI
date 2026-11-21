# Native 9.27 integrated acceptance

Frozen October 2, 2026 after actual packaged-app interactions. **Not release
ready:** New conversation still crashes through accessibility label recursion;
a short ordinary conversation exceeds the request byte budget. Successful reply,
review, persistence and shutdown results do not close those gates.

## Exact candidate and isolation

- App: `desktop-native/build/FERAL Native Preview.app`, version `2026.9.27`,
  build `2026100201`, bundle ID `ai.feral.native.preview`.
- Frozen source: `34a43e640597ca721fb915149f29c9b73b51394a`. Subsequent
  CORE-04/SDK working-tree changes are excluded from this candidate.
- Executable SHA-256:
  `1071d7b679730a7d2cdca09c422aba4b65a6ef94ada6b8357a10ad8fcc4556d0`.
  Independently checked before launch and before every REST probe.
- Parent audit: all 478 bundled tracked production Python files match frozen
  Git blobs, zero missing/mismatches. Bundle audit passed: 13,052 files, 265
  Mach-O files, nine internal links, Python 3.11.15, SQLite 3.53.1, FTS5,
  loadable extensions and OpenCode 1.18.10. These are audit results, not GUI tests.
- Parent receipts: `/private/tmp/feral-candidate-9-27-manifest.json` and
  `/private/tmp/feral-native-9-27-bundle-audit.json`.
- Disposable root: `/private/tmp/feral-native-populated-20261002`. Separate HOME,
  TMPDIR, FERAL_HOME, DATA_HOME, and defaults suite
  `ai.feral.native.acceptance.6d29504e06625a63`; no personal profile/defaults.
- First host/child: `79839` / `79846`, port `59212`. Relaunch: `89891` / `89925`,
  port `61484`. Exclusive CUA GUI ownership. No rebuild/restaging.
- Existing owned Ollama `48745`, port `11436`, installed alias
  `theora-coding-442789f441d80043`, Qwen2.5 3.1B Q4_K_M. Actual server reported
  context 16,384. No download, cloud credential or account connection.

## Actual results

| Gate | Result and evidence |
| --- | --- |
| Populated recovery | Passed for tested data: initial 57 conversations; rich thread 15 records, renamed title, pinned state and exact first three original records. |
| Original connection disclosure / rich metadata | Actual AX and screenshots tested. Disclosure initially returned one AXError.failure; host remained alive and next full AX read succeeded with expanded recovery text. Reasoning, usage and original tool metadata rendered. No SIGSEGV on that bounded path; transient AX error retained. |
| Rich to short switching | Passed: continuing acceptance-history-54 replaced rich content with its two original messages. |
| Selection / Copy / link | Passed in actual package using synthetic display fixture: selected user text copied exactly; Copy code pasted let acceptance927 = true; bold, italic, inline code and fenced code rendered. Markdown link opened https://example.com/ in Chrome; only owned test tab closed. Fixture rendering is separate from model generation. |
| Real local reply | Passed: native arithmetic request 13 plus 29 returned and persisted 42. Request 28,998/32,000 bytes; estimated input 11,435 + output reserve 1,024 + template reserve 256 against context 16,384; tokenizer unverified. Minute-scale prefill observed, not a latency acceptance pass. |
| Pending truthfulness | Passed for both new model requests: response said sent for approval, without claiming file creation. Historical 9.26 false claims retained as historical evidence. |
| Wrong fallback prevention | Passed for observed budget refusal: no unrelated Weather or other fallback action ran. |
| Headless proposal to native review | Unavailable under current profile policy. Tool execute with confirm:true returned success:false, status403, surface:http_api, level:deny, surface_deny:true. Queue zero and file absent. No bypass or claim of headless review success; model-driven tests below are distinct. |
| Native Deny | Passed: request 1730e6b0-5768-4cca-9178-2bf9a0320ba0, session acceptance-history-54. Exact path/content reviewed in Oversight; native Deny confirmation removed request, persisted cancellation, independent denied-file readback absent. |
| Native Allow | Passed: fresh session acceptance-9-27-formatting, request 2d0adc71-c1c1-430c-bf6d-f181978b12f9. Exact path/content and file absence checked before native confirmation. Result 21 bytes, checkpoint cp_2379b67b3ec14d55, turn turn_0b5e324f8945400a; independent file readback exactly FERAL_9_27_ALLOW_ONLY. Queue zero; denied file absent. |
| Folder permission | Passed for disposable project only. Security & Cost selected exact folder, reviewed readwrite grant, confirmed and displayed refreshed grant. Initial grants empty. No personal folder or OS permission change. |
| Pause / confirmation Cancel / Resume | Passed: Cancel left Running; Pause showed Paused. Harmless arithmetic chat declined as supervisor_paused, displayed delivery failure and audit denial. Resume restored Running. Stale global error banner remained after resume, separately from actual pause state. |
| Stop response | Partial: after relaunch harmless sky explanation showed Thinking; Stop cleared busy state/composer and disconnected/reconnected same session. Backend logged no assistant row. No correlated terminal cancellation receipt or provider-stop measurement in this candidate; exact backend cancellation open. |
| New conversation | **Failed: confirmed SIGSEGV**, below. Cancellation request had not started at first crash. |
| Crash cleanup / relaunch persistence | Original host exit -11 and backend absent. Relaunch retained avatar/name, rich title/pin, all15 records and exact original3 metadata; short10 records including real reply/refusal; formatting7 records including actual success and refused user request. Independent allowed-file content / denied absence persisted. Total59 includes new cancellation thread. |
| Normal quit / child cleanup | Passed after relaunch: CUA Cmd-Q; observer host89891 exit0; graceful shutdown and Finished server process89925 logged; scoped ps returned no host/child rows, exit1. No CUA rebinding to dead app. |

## Confirmed failures and limits

**New conversation crash:** after Allow, pause/resume and inspection of the saved
budget failure, clicking Conversations > New conversation created a backend
thread (POST /api/conversations/new 200) and switched its WebSocket. Following CUA
AX observation reported App quit. Held observer recorded host79839 exit-11.
Apple report `feral-native-2026-10-02-094728.ips` identifies that PID, exact9.27
version/build, capture `2026-10-02 09:46:19.3312 -0700`, EXC_BAD_ACCESS/SIGSEGV,
KERN_PROTECTION_FAILURE and “Thread stack size exceeded due to excessive
recursion”. Main-thread frames repeat SwiftUI AccessibilityNode.accessibilityLabel,
labelsToResolve, labelExposedAs and AppKit _NSAccessibilityOverrideValueForAttribute.
This confirms the crash class, not which remaining production view owns the
recursive node. No speculative source fix was made during acceptance.

**Request budget:** after arithmetic plus one denied tool request, another normal
file request in the short session hit system18,545 + history1,888 + schemas11,692,
total **32,146 > 32,000**. Streaming retried normal command, which hit the same
guard. Model server/alias was healthy. Native reply nevertheless instructed
reconnecting a model in AI Providers. That misidentifies the failure. Refusal
prevented fallback effects; everyday capacity and error remain release blockers.
Fresh-session Allow fit at30,259 bytes and succeeded; it does not fix the failing
existing session.

Other observations: approval prose points to Devices/Approvals, while native
reviews are in Oversight. Historical pending/running events remain alongside a
terminal outcome rather than updating in place. Memory warnings include absent
cached fastembed model, sqlite-vec cross-thread connection fallback, invalid-value
normalization and unawaited episode_recent coroutine during disconnect/shutdown.
Working health/stored records do not prove semantic-memory correctness. Docker
absent. Remaining172 SwiftUI selectable expressions across28 feature files are
outside the prior narrow chat repair; broader accessibility coverage stays open.

No financial action, real account, external message, purchase, private credential,
OS permission, clean-machine install, Linux or physical glasses test performed.
Attachment deny/allow and history rename/pin/search tests belong to the original
9.26 run; not repeated here. Their retained records/title/pin were checked here.
See [original report](NATIVE_POPULATED_ACCEPTANCE.md) and
[narrow repair evidence](NATIVE_ACCESSIBILITY_EVIDENCE.md).

## Reproduction and retained evidence

Commands run from ASOS against disposable root:

```bash
python3 desktop-native/acceptance/populated_profile_probe.py \
  --root /private/tmp/feral-native-populated-20261002 \
  --app 'desktop-native/build/FERAL Native Preview.app' --hold
python3 desktop-native/acceptance/integrated_acceptance_probe.py inspect
python3 desktop-native/acceptance/integrated_acceptance_probe.py seed-formatting
python3 desktop-native/acceptance/integrated_acceptance_probe.py propose-deny
python3 desktop-native/acceptance/integrated_acceptance_probe.py readback
ps -p 79839,79846,48745 -o pid,ppid,etime,command
ps -p 89891,89925 -o pid,ppid,etime,command
```

Launcher refuses a duplicate live recorded host. REST aid verifies root, port,
executable hash, native ownership and exact bundled child command. Inspect was
run once before mutations; do not overwrite baseline on final replay. Display
fixture is synthetic. Headless proposal was refused. Model prompts/reviews,
selection/link/pause/quit were actual CUA interactions.

Local artifacts below are under the disposable root, not shipped or committed:

- 9-27-first-launch.json, 9-27-initial-readback.json, 9-27-deny-proposal.json,
  9-27-allow-predecision.json, 9-27-allow-postdecision.json, 9-27-relaunch-readback.json.
- 9-27-short-chat-readback.json, 9-27-formatting-chat-readback.json,
  9-27-crash-exit.json, 9-27-crash-stack.json, 9-27-crash-backend.log,
  9-27-normal-quit-exit.json, 9-27-normal-quit-backend.log.
- Actual screenshots: 9-27-rich-recovery-metadata.png, 9-27-local-real-reply.png,
  9-27-formatting-selection-copy.png, 9-27-allow-native-outcome.png,
  9-27-pause-refusal.png, 9-27-context-guard-ui-mismatch.png,
  9-27-relaunch-rich-fields.png.

Synthetic profile/file/evidence retained for next repair; owned Ollama48745
available to coordinator. Both native hosts and backend children are stopped.

Probe validation: pinned `.venv/bin/python -m py_compile` passed; pinned Ruff
`--select=E,F,W --ignore=E501,E402,F401,W291,W293` passed; scoped `git diff --check`
passed. These validate the manual aid, not native product behavior. Persistence
was checked after the confirmed crash/relaunch; no third launch after the final
normal quit was performed.
