# Mac provider review and committed context integration

October 3, 2026. Source and fixture verification; not account, microphone or
packaged GUI acceptance.

## Provider confirmation

The baseline allowed changed drafts to dispatch, a reused review to dispatch
twice, and changed saved configuration to retain an old confirmation. The repair
binds each single-use review to the connection, draft, saved configuration,
provider descriptor, public key metadata and credential-storage state. Reviews
expire after five minutes using monotonic time. Cancellation, connection drift
and changed readback refuse dispatch; uncertain effects require fresh state
inspection rather than retrying the review. Credential-free local setup remains
available. Default requests reject redirects and unexpected response origins.

The integrated `bash test_features.sh Providers` runner passed21 provider fixture
groups/33 assertions,27 linked-model groups,39 desktop assertions and five error
presentation assertions. These use mocked HTTP/wire, not real credentials or
accounts. Provider source SHA256:
`ad700f64236bd8c383895355068be8926669c3ea4598d87caf0f2843c53709de`.
Test source SHA256:
`b60ad44b82406fd3bca36d9be91347357711599bab7aab7cf7500d735427aa99`.
Fresh GET verification is not a backend atomic compare-and-swap; another client
may change configuration between the final read and mutation.

## Attachment-bound committed turns

Tracked turns retain their original attachment, store, coordinator and socket
owner throughout preparation and checkpoint commit. An operation-local receipt
captures the exact verified context fence. Successful, approval and refusal
terminals may include that historical checkpoint; other outcomes cannot certify
it. Replacement ownership suppresses stale live certification. Historical
status remains available and must never authorize automatic replay or playback.

Original-source refinement reproduced one synthetic provider invocation after
socket-owner replacement. Corrected registered-route cases dispatched zero
provider invocations for owner/store/coordinator/refusal replacements. This does
not establish atomic cancellation of an external effect already in progress.

Twenty-eight related suites passed567 tests/303 warnings in29.17s. All1,288
source/config inputs remained unchanged, digest
`1b2836bd27538bf17fec69a32350bd3a1cedd93b5a929752169945344e9cc7b9`.
Same-environment nonincremental configured mypy compared published `f41c204fe`
with the working source:812 diagnostics in each, zero added or removed. Existing
diagnostics remain; this is not a zero-error typing result. Incremental counts
varied and are superseded by the nonincremental comparison.

Private test receipts and logs use prefix
`feral-attached-turn-integration-20261003`; no profiles or databases are published.
Managed saved-context voice remains refused until its per-utterance tracked-turn
adapter and native media contract are implemented. Candidate9.34 remains its
immutable preceding source, without these changes.
