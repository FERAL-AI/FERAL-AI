# Chained voice output interruption

October 2, 2026. The correction reuses the chained pipeline and router; it adds
no command dispatcher or memory store.

## Reproduced behavior

The client response-interrupt route called whole-turn cancellation, taking down
the running command when the user only interrupted speech. A held synthetic
streaming command reproduced that cancellation. Separating output then exposed
a second real endpoint bug: while the first command was held, another audio
buffer/VAD end/silence endpoint arrived. After completion, that next utterance
remained buffered forever unless a further packet arrived. Both baseline logs
are retained in `/private/tmp/feral-chained-interruption-baseline.log` and
`/private/tmp/feral-chained-overlap-baseline.log`.

## Result

Response interrupt and VAD barge-in now suppress speech immediately and cancel
only the owned synthesis task. The agent command keeps running. Exact live
session and turn ownership suppress even cancellation-resistant late audio.
The output-cancel frame explicitly reports speech scope and no agent-task cancel
request. Explicit task cancellation and session teardown retain full cancellation
semantics; already-started external effects are not rolled back.

Endpoint signals during a running turn coalesce into one pending flush using
existing transcript/audio buffers. The next utterance starts sequentially only
after that exact live owned turn succeeds. Stop, command failure, cancellation,
close and replacement discard automatic admission and disarm the silence timer.
No failed or interrupted command is automatically replayed.

## Checks and boundaries

The frozen eight-suite worker run passed **120 tests**, including 13 new
interruption/admission cases. Synthetic streaming commands, cooperative and
late-yielding synthesis, VAD/real buffer ingress, repeated/foreign interrupts,
next utterance, explicit cancel, teardown and failure are covered. Maximum
simultaneous command execution in the overlap fixture is one. Scoped Ruff and
whitespace checks passed. Scoped typing retained six existing messages with
zero added messages. Fresh whole-core mypy retains **811 diagnostics**, with
zero added/removed normalized messages against verified 61550e7. All 1,274
inputs were unchanged before/after; input digest is
`593f3723fdcfc98929f0ad4e1d78edfe89d4c10c62cbe50daa78dfb05e991e80`.
Whole-core configured Ruff passed. The unchanged baseline ceiling remains 812;
this does not mean zero type debt. Whole-core log and hash manifests are under
`/private/tmp/feral-voice-archive-mypy-20261002`.

Parent combined archive/config/checkpoint/sandbox plus those same voice suites
passed **279 tests**, 71 warnings, 13.25 seconds. Actual logs:
`/private/tmp/feral-chained-interruption-final-tests.log` and
`/private/tmp/feral-voice-archive-integrated-20261002.log`.

These are fixture tests, not real microphone/audio acceptance. Native wire
normalization, opening a chained session before ACK, attempt/response authority
for realtime replacement and managed saved-context voice remain subsequent
contracts. Existing managed-voice refusal is preserved. Candidate 9.32 does
not contain this later source correction.
