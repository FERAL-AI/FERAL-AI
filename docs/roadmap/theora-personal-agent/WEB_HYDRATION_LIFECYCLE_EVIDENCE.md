# Web conversation hydration lifecycle

The published `86a8b54ed273642da40776e75609f5a45a197924` backend CI passed
12,871 tests, with 83 skipped and 75.10% coverage. Native and the unchanged
type-count gate also passed. The web job failed despite 1,358 passing tests:
React attempted a state update after the test browser closed. Its stack named
`Shell.jsx` conversation publication, fallback creation and boot hydration.
That failed job is retained; passing individual tests was not a green CI run.

The shell's hydration cancellation flag previously guarded only the primary
session/conversation ID updates. Active conversation publication, transcript
merging and fallback creation still continued after asynchronous requests.
The one-shot ref also prevented the current StrictMode effect lifetime from
hydrating after the retired effect was cleaned up.

The correction gives each hydration lifetime its own AbortController and
cancellation check after every awaited stage. Conversation creation also checks
that its originating hydration lifetime is current before reading or publishing
the result. Cleanup retires that lifetime and aborts its requests. The current
StrictMode lifetime starts its own hydration. This does not claim that aborting
a request reverses a server-side conversation already created.

## Reproduction and verification

Five new deferred-response cases failed against the preceding shell:

- Closing during primary-session resolution continued the remaining boot chain.
- Closing during active-thread loading could overwrite the remembered thread.
- Closing during transcript loading continued fallback creation.
- Closing while conversation creation was pending published its late result.
- StrictMode's current lifetime never hydrated while the first request was pending.

Baseline log: `/private/tmp/feral-shell-hydration-baseline-20261002.log`.
The corrected implementation passed all five cases and the existing consumer
shell cases: eight tests in two files. All shell suites then passed:
**201 tests in 20 files**, exit zero, 6.51 seconds.

Logs: `/private/tmp/feral-shell-hydration-fixed-20261002.log` and
`/private/tmp/feral-shell-lifecycle-suite-20261002.log`.
The assertions check that retired responses neither continue boot requests nor
replace the remembered conversation. Request cancellation is not used as the
sole defense: the fixture deliberately resolves responses after cleanup.

The complete web coverage job on the correction remains required. These tests
do not establish actual Mac GUI/audio acceptance. Immutable candidate 9.33 and
the concurrently running browser snapshot contain the preceding `7f818da08`
source and exclude this correction.
