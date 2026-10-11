# Voice startup and callback ownership evidence

October 2, 2026. Extends the existing voice router, chained pipeline and realtime
engines. No new dispatcher, memory store or provider authority is introduced.
The accompanying commit follows published `55a99aba7e1b83663ab3cb200ab05d2cd9fc8d3b`.

## Resulting behavior

- WebSocket and gateway voice configuration share the existing runtime/session
  lock and verify the current connection owner before and after asynchronous
  work. Chained mode is accepted by the canonical schema. Startup failure returns
  an explicit error instead of an affirmative acknowledgment.
- Chained startup uses saved operator configuration, prepares recognition before
  acknowledging it, and bounds recognition preparation to ten seconds. Failed,
  cancelled or superseded preparation closes only its unpublished providers.
  Deepgram connects once before the first microphone bytes are sent; closing
  during connection cannot resurrect the transport.
- OpenAI and Gemini callbacks retain the exact engine instance that registered
  them. A retired engine cannot forward audio, transcripts or cancellation to its
  replacement. OpenAI response IDs additionally fence obsolete response events.
  Interrupted callbacks that ignore cancellation cannot publish obsolete results.
- Native playback accepts the chained pipeline's actual `audio_chunk` wire type,
  validates ordering and the empty final sentinel, and clears queued playback on
  the existing speech-only cancellation frame. Task cancellation remains separate.

Failure cleanup uses exact returned-instance ownership. A changed SID entry alone
does not prove that the failing invocation owns a replacement. Existing managed
voice-start refusal and permitted Stop behavior remain explicit.

## Checks actually run

These counts overlap and must not be added into a single product coverage number.

| Check | Result | Local evidence |
|---|---|---|
| Engine ownership and related voice regressions, 19 suites | 285 passed, 2 skipped, 18 warnings; 28 new regressions | `/private/tmp/feral-engine-callback-freeze-tests-20261002.log` |
| Shared configuration, actual route dispatch and compatibility, nine suites | 156 passed, 2 skipped, 263 warnings | `/private/tmp/feral-client-voice-config-integrated-final2.log` |
| Recognition startup, chained pipeline and speech interruption after transport typing | 57 passed, 6 warnings | `/private/tmp/feral-chained-stt-startup-typed-final.log` |
| Parent combined configuration/protocol/engine/startup checks before final transport typing | 183 passed, 72 warnings | `/private/tmp/feral-voice-startup-integrated-20261002.log` |
| Canonical protocol, constraints and hardware protocol | 63 passed, 34 warnings | `/private/tmp/feral-chained-wire-protocol-tests-isolated-20261002.log` |
| Native voice fixtures | 44 voice assertions plus linked-model, desktop and error checks passed | `/private/tmp/feral-native-chained-wire-tests-escalated-20261002.log` |
| Production Swift typecheck | Passed | `/private/tmp/feral-native-chained-wire-typecheck-escalated-20261002.log` |
| Whole-core Ruff | Passed under configured rules | Recorded with the frozen typing run |
| Whole-core mypy, frozen inputs | 808 errors; zero added and three removed versus verified `55a99aba` diagnostics | `/private/tmp/feral-voice-startup-typed-mypy-comparison-20261002.json` |

All 1,278 typing inputs stayed unchanged, with digest
`94d837e9582b9f875a7d4d96329ad22eef583439c6b45cfe84eddf592ef3da21`.
The existing 812-error baseline and configuration were not relaxed. This is a
regression check, not a claim that the repository has zero typing errors.

Eight inert recognition-startup regressions cover delayed connection/first audio,
failure, timeout, cancellation, replacement, close races and an unavailable
post-prepare transport. Seven initial cases failed before the implementation;
their baseline log is `/private/tmp/feral-chained-stt-startup-baseline-2.log`.

Earlier failed checks remain available: one configuration fixture returned a
different mocked instance than the instance opened; its exact-instance fixture
was corrected. An initial full typing run had 811 errors but one added diagnostic;
proper transport typing and a post-prepare guard repaired it. Counts alone did
not close that gate. Sandbox-denied native macro checks were rerun with approval.

## Confirmed previous publication

Exact-head checks for `55a99aba` passed remotely: backend 12,783 tests, 83 skipped,
74.93% coverage; native production checks, web and developer SDK checks passed.
Remote typing retained the prior 811 diagnostics against the unchanged baseline.
CI run `37099311201` and native run `37099311193` concern that publication, not
automatically this later wave.

## Separate acceptance gates

No live provider or microphone session was exercised by these fixtures. The two
skipped engine checks require real provider access. Realtime configuration that
remains lazy does not claim provider inference merely because its ACK succeeded.

Same-SID client attempt/ACK fencing, durable managed-context voice support and
native output-generation correlation remain additional work. Gemini provides
interruption/turn signals but no equivalent wire response ID; local epochs cannot
identify every arbitrarily delayed unmarked packet. Already executed external
effects cannot be rolled back by suppressing an obsolete callback.

Exact publication `7f818da08139952b1698644469e7016563512cd5` passed native,
web/SDK and typing checks (remote808 exactly matches local). Backend CI run
`37102142116` failed with12,867 passed/3 failed/83 skipped/one setup error and
75.10% coverage. Two fixtures lacked the actual connected/owner state now required;
their corrections pass132 focused tests without changing production guards.
The module guard now copies sys.modules before iteration to avoid background
import mutation; the affected ambient suite passes16 tests. Full repaired CI is
still required. These later fixture results do not erase the failed publication.

Candidate 9.32 contains `61550e74f`, excludes this wave and remains preserved at
`/private/tmp/feral-candidate-9-32-preserved.app`. Candidate9.33/build2026100207 is
assembled from exact7f, with485 production files matching, strict ad-hoc signature
and runtime audit passed. ExecutableSHA256
`c54b8ec05159ccf236bd27e5dd79e927539cc13d7ffe102d192cc2316801058e`.
Actual attempt2 reached Stop/recovery/real local-model recall but failed restart
readiness at100s; cause remains unresolved. Attempt1 was sandbox-denied. Both
disposable records are retained;9.33 is not accepted by a partial journey.
Computer Use still fails native-pipe startup, so current GUI/audio acceptance is
open. Signing, clean installation, upgrade and physical-device gates remain open.

## Real-browser run integrity

An isolated live-backend browser run completed 60 checks. Its post-run source
comparison found missing files in the copied tree, so it failed the immutable
source gate. The original checkout still contained the tracked files. The cause
was not established; this result is not certified current-source acceptance.
Failure evidence is retained under
`/private/tmp/feral-live-web-20261002-zstq61eb/source-freeze-FAIL.json`.

The rerun uses only tracked files from an exact committed source, a read-only
snapshot after building, module provenance checks, and a fresh disposable runtime.
A keyboard-only skip link receives a dedicated keyboard check. Unexplained dead
controls and overflow are still reported; a successful walk is not proof that
every reported control is functional.
