# Phone-origin durable task creation

The existing ChatTurnManager now carries its private authenticated phone identity
through direct and reviewed TaskFlow creation. A tracked phone turn can create
one durable task while the native client retains shared-conversation routing
ownership. Session membership or a supplied device identifier grants no authority.

The private principal travels beside the existing public task_origin, using a
creation guard. It is excluded from dataclass projections, model context, flow
responses and wire receipts. TaskFlow adds a nullable creation-owned storage
column; old NULL rows are not attributed to a device. Current credential and
turn/runtime ownership are checked during receipt lookup, deduplication, commit
and result publication. Changed terms or foreign provenance cannot reuse a
handoff. Revocation before commit rolls back creation.

The background_task skill's status/list paths filter by originating conversation
and authenticated device. The operator retains existing global views. Paired
credentials cannot reach raw TaskFlow operator CRUD/control routes through the
existing middleware. This patch does not expose those routes remotely.

A fresh fully verified credential for the same still-paired device can inspect
its stored flow after reopen. Stored identity is provenance, not an approval or
a restored live socket. Re-pairing with a different device ID gives no automatic
claim on prior work. Local operator reviews remain usable.

Mac 9.51 kept paired approval resolution unavailable. Mac 9.52 adds private exact
review publication, guarded REST decisions and explicit waiting model-review
renewal; [current contract](PHONE_REVIEW_AUTHORITY_20261010.md). Job-scoped Chrome
permission and physical iPhone acceptance remain separate work.
Creating a task or returning its processing receipt does not prove its external
goal was achieved.

Focused verification passes 144 tests across seven suites using actual CTM,
SQLite, backing tasks, local operator HTTP review and paired middleware with inert
effect boundaries. It covers direct shared-owner creation, exact retry, reviewed
handoff, foreign-source refusal, spoofed private fields, NULL migration/reopen,
credential rotation, before-commit revocation and post-read cancellation. Affected
typing and lint checks pass. One existing baseline case attempts a model-hub
lookup that is blocked by the test network guard; no live model is used.

This source is included in immutable Mac 9.51; it is absent from 9.50.
Current integrated checks, packaging and publication are recorded in
[WORK_STATE](WORK_STATE.md).
