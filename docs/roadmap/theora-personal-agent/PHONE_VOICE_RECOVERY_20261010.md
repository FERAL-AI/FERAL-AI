# Phone voice and control recovery

October 10, 2026. Integration base: `0cf4cb650274513eea5c284c10f9851089534b93`.

The repaired iOS development app is installed in place on the connected iPhone
17 Pro Max from local commit `5749babc52087698de21aec96e0926650a823f37`.
568 Foundation assertions, actual Swift-to-Mac ambient schema validation, unsigned
and signed device builds, strict signing verification and installation pass.
iOS commits remain unpushed. The app has not been launched for physical retesting.

Mac voice startup now leaves registered control intake responsive, orders queued
audio behind startup and fences exact socket, credential, runtime and producer
ownership. Retired attempts cannot feed or stop replacement sessions. Model-step
Chrome authority and completed/deferred traces include the published 0cf4cb650
repair. Four remote Ubuntu failures are repaired in test fixtures without changing
production authorization or catalog guards.

Frozen integration passed 1,237 tests/four skips and full-core CI Ruff. Two new
Mac typing diagnostics were corrected with equivalent dynamic attribute stores;
the final focused gate passes 217 tests, full-core CI Ruff and retains 788 baseline
Mac diagnostics with zero additions/removals and zero source drift.

Mac 9.56/build2026101005 assembly is next, using verified unchanged 54-input
native output from 9.55. The existing 9.55 bundle does not contain this later
backend wave. Known-good 9.52 remains the sole generated rollback. Actual live
provider, microphone/HFP and browser/result/reconnect acceptance remains open.
The prior page-control receipt is cancelled with unknown action outcome; reconcile
it without replay. Code 9 still has no established termination cause.

## Reproduced defect and repair

The real registered paired `/v1/node` route previously awaited provider startup
inside its receive loop. An inert delayed provider reproduces blocked heartbeat
and action-response intake. The repair retains a bounded FIFO startup/audio lane
and uses the existing VoiceAttemptBinding producer scope without changing node
wire identity, grants, provider selection or replay policy. Exact old producer
retirement avoids global stop/transcript publication against a replacement.
Repeated cancellation-resistant startup remains bounded and unavailable until
it actually settles; the next normal idle recording starts on its first request.

Fourteen new registered-route cases cover delayed startup, FIFO audio, stale
streams, credential/runtime revocation, resistant replacement, bounded retries,
backlog failure, configured chained fallback, same-socket manifest refresh,
normal next recording and real RealtimeProxy handle replacement. The independent
worker gate passes 75 tests and frozen source review releases the patch.

The observed late socket replacement is a separate fact; this startup defect
does not establish its cause. The original incident's cancelled/unknown receipt
is preserved. No task, approval, browser mutation, voice recording or provider
call is replayed during these tests or installation.

## Retained evidence and acceptance

The frozen source snapshot is
`/private/tmp/feral-phone-final-integration-20261010-elnkmusn`.
Its `combined-evidence` retains 1,237 passing tests, four skips and the initial two
new typing diagnostics. `final-focused-evidence` retains the corrected final
source hashes, 217 passing tests, CI Ruff and 788 unchanged baseline diagnostics.
A separate 14-case real model-loop consent gate also passes on this source.
The three CI fixture files pass 128 tests in their five complete suites before
integration. Fresh remote Ubuntu CI is required after publication.

Physical acceptance must use the identified installed phone and current Mac
candidate: one explicitly requested harmless page interaction, observed effect,
terminal receipt and spoken/text answer, then route changes and disconnect/
reconnect recovery without goal resend. Installation and fixture success are
separate from those device and provider checks. If code 9 was not an explicit
Xcode stop, inspect a device termination report before assigning a crash cause.
