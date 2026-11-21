# Native 9.28 populated acceptance

Frozen October 2, 2026 after actual isolated acceptance. **Failed overall:** the
original retained-error New conversation AX path passed, but a distinct Security
and Cost layout crash interrupted the matrix. This is not release certification.
[Previous9.27 evidence](NATIVE_9_27_ACCEPTANCE.md) remains unchanged.

## Candidate and isolation

App: desktop-native/build/FERAL Native Preview.app, version2026.9.28/build2026100202,
bundleID ai.feral.native.preview. Executable SHA256:
`dba3785d53634fb699986c2ade9290f42ff5c0631c63224510bfb34350d165f0`.
Frozen runtime source: `66c7cd500d7ec353c68da97f44b99d6201af9abe`.

Coordinator reported stage/build/codesign verify exit0; all479 production Python
files matched that commit. Parent audit passed13,054files/265MachO/9internal links,
Python3.11.15/SQLite3.53.1/FTS5/extensions/OpenCode1.18.10. Receipts:
`/private/tmp/feral-candidate-9-28-manifest.json` and
`/private/tmp/feral-native-9-28-bundle-audit.json`. Worker independently checked
executable hash and Info.plist before launch and hash at freeze. No rebuilding,
restaging, artifact replacement or production edits during these tests.

Reused only synthetic profile `/private/tmp/feral-native-populated-20261002`, with
distinct HOME/TMPDIR/FERAL_HOME/FERAL_DATA_HOME and preferences suite
`ai.feral.native.acceptance.6d29504e06625a63`. First host23301/child23312/port49626;
relaunch27774/child27781/port50649. Probes verify exact hash/receipt/child command
and PPID before loopback API access. Vault stayed locked; federation dormant;
no account, credential, recording, external message or purchase action.

Existing owned Ollama48745:11436 selected alias
`theora-coding-442789f441d80043`; actual /api/ps verified3.1B Q4_K_M and context16384.
No downloads or full bundle copies. Disk6.1GiB before launch,4.0GiB during tests;
no personal/cache cleanup. Health reported core2026.9.7, distinct from native9.28.

## Actual matrix

| Gate | Result |
| --- | --- |
| Retained-error New conversation AX | Passed Pause refusal, Resume, Conversations > New conversation, expanded Error details with exact supervisor_paused and new isolated Conversation connection |
| Supervisor Cancel/Pause/refusal/Resume | Passed Cancel retained Running, confirmed Pause caused deliveryfailed supervisor_paused, confirmed Resume restored Running; exact audit JSON reason matched |
| Actual local reply | Passed13+29 produced42 in screenshot/full AX and exact saved synthetic thread |
| Three-plus completed turns without restart | Not passed: second CAFE928/Ready request remained unanswered at first exit; file request completed after relaunch, next Allow interrupted by crash |
| Growing context beyond byte limit | Partial: next Allow logged32692>32000 then narrower-schema31862 and Ollama preflight; no task outcome before crash |
| Exact native Deny | Passed dfb3ae36-912c-478d-ad59-8be6d294347c, thread-70fb2086-2, coding_tools__write_file, unique9-28-deny-only.txt/FERAL_9_28_DENY_ONLY; confirmation removed queue/rendered Cancelled; file remained absent at final direct inspection |
| Native Allow/write readback | Not completed; unique9-28-allow-only.txt absent at freeze; no success claimed |
| Rich original metadata retention | REST passed original three records unchanged,15records, pinned renamed title; initial59conversations, later61 with new fixture/thread |
| Rich/short GUI switch, Copy/link/wrapping | Not run9.28; earlier9.27/component evidence remains distinct |
| Migrated read-only pages | Oversight exact audit and Credential vault text survived AX; Permissions and cost tab caused SIGTRAP |
| Stop response | Not run9.28; legacy termination-receipt limitation remains open |
| Deliberate normal quit/relaunch | Not run; relaunch after unexplained exit restored42 and unansweredCAFE928 row |
| Child cleanup | Both owned hosts/children absent after exit; second backend log gracefully shut down and Finished server process27781 |

## Failures

1. **Early timeout:** arithmetic entered preflight11:03:15 and saved42. CAFE928
   entered preflight11:05:23; immediately after its send, within18seconds, AX
   showed “The model has not replied within four minutes” while Thinking and
   second llm_call were present. Exact error disclosure retained it. Parent
   identified a possible stale deadline race by source inspection; that is a
   hypothesis separate from observations. No Ready reply invented.
2. **Unexplained exit0:** host23301 observer returned0 without this worker issuing
   Quit/signal. Log11:06:39 disconnected the synthetic session; pending turn had
   no assistant row. Parent reported no other worker signal/artifact replacement.
   No attributable new IPS; cause unresolved, not called a crash.
3. **Confirmed distinct crash:** Security and Cost > Permissions and cost tab
   after read-only vault inspection returned CUA timeout. Host27774 observer-5.
   IPS feral-native-2026-10-02-111537.ips identifies PID27774, capture11:14:09.406
   -0700, EXC_BREAKPOINT/SIGTRAP, main-thread NSApplication._crashOnException,
   AppKit constraints/displaycycle. asiBacktraces connects NSTextField.updateConstraints
   and content-size constraints through SwiftUI layout invalidation to
   NSWindow._postWindowNeedsUpdateConstraints. Scoped OS log11:14:08.082 further
   identifies NativeSelectableTextField.fittingSize(width:field:) invoking
   NSTextField.invalidateIntrinsicContentSize during sizeThatFits. This layout
   failure differs from9.27 recursive accessibility-label SIGSEGV. Exact exception
   reason/minimal reproduction are followup work, not claimed repaired here.
4. **Stale pending cards:** after actual Deny/cleared queue, chat retained duplicated
   pending_approval tool rows/cards alongside assistant Cancelled. The card's
   backend-reported-status caveat remained; it is not an independently verified
   final outcome.
5. **Target after death:** later getAXState on the existing binding returned
   onboarding; scoped process inventory found same-binary PID29668/PPID1. A
   refetch may have relaunched defaults; causality unproven. GUI stopped immediately.
   No onboarding/import/reset action. After exact PID/path identification, only
   PID29668 was stopped with TERM. Future observations must check owned liveness
   before using a dead target; preserve defaults.
6. Isolated embedding cache absent; sqlite-vec logged thread-affinity fallback to
   numpy. No download or vector-acceleration claim. Accounts, physical hardware,
   voice, Linux, signing/notarization and clean-machine install remain untested.

## Evidence and commands

CUA native click/typeText/super+Return and fresh AX were actually used. A full AX
refresh resolved a stale differential snapshot; screenshot/full tree both showed42.
Synthetic JSON, screenshots, logs and observer receipts remain outside Git under
the disposable root:

- 9-28-initial-readback.json, 9-28-review-readback.json, 9-28-selected-chat-readback.json,
  formatting/short readbacks.
- 9-28-new-conversation-error.png and9-28-real-reply.png.
- 9-28-first-launch.json, 9-28-first-launch-exit.json and9-28-first-launch-native-desktop.log.
- 9-28-second-launch.json, 9-28-second-launch-exit.json and9-28-second-launch-native-desktop.log.

OS IPS stays in read-only DiagnosticReports; raw IPS not committed. Commands run
from ASOS:

```text
python3 desktop-native/acceptance/native_9_28_launcher.py
python3 desktop-native/acceptance/native_9_28_probe.py inspect
python3 desktop-native/acceptance/native_9_28_probe.py seed-formatting
python3 desktop-native/acceptance/native_9_28_probe.py readback --thread thread-70fb2086-2
curl -sf http://127.0.0.1:11436/api/ps
ps -p 23301,23312 -o pid,ppid,etime,command
ps -axo pid,ppid,etime,%cpu,%mem,command
/usr/bin/log show --style compact --start '2026-10-02 11:14:08' --end '2026-10-02 11:14:11' --predicate 'processID == 27774'
```

Process inventory output filtered to native binary/backend identities. Observer
sessions16956/40111 returned0/-5. Both effect files absent and executable hash
unchanged at freeze. Python probe syntax and whitespace checks passed. These three
acceptance files are frozen; layout investigation uses separate evidence/assets.
