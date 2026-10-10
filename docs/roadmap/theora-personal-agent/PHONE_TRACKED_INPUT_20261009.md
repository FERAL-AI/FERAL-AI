# Paired phone input receipts

## Implementation boundary

This source wave extends existing phone ingress and ChatTurnManager. It does
not replace the orchestrator, executor, TaskFlow or memory. Negotiated paired
inputs gain durable processing receipts and private authenticated device identity.
Legacy messages retain their response contract. Actual installed iPhone and
model-selected Mac task acceptance remain separate from isolated route tests.

Mac 9.50/build2026100904 contains this runtime at exact source 5f488ba8a.
Package identity, bundled methods and native lifecycle acceptance pass;
[executed acceptance and remaining gates](NATIVE_9_50_ACCEPTANCE.md).

## Additive client contract

For a committed chat input, send an explicit canonical lowercase UUID in the
top-level msg_id and integer turn_contract_version 1 in the payload:

```json
{
  "type": "chat_request",
  "msg_id": "7cf9d173-7a6c-4c0c-bdea-8cc6aa2fe496",
  "payload": {
    "turn_contract_version": 1,
    "session_id": "the-existing-conversation",
    "text": "Read the selected page on my Mac",
    "channel": "chat",
    "device_target": "brain",
    "reply_mode": "final",
    "reply_to": "client-display-correlation"
  }
}
```

Authenticate using the existing paired token or phone bearer. The server derives
device identity from verification; fields in context, node aliases, session IDs
and broadcast membership cannot supply it. Legacy shared node keys do not support
the negotiated device identity. Missing/generated, noncanonical IDs and invalid
versions are refused before tracked execution.

The first input commits chat_turn_accepted before running the existing phone
preparation/agent path. A first chat_request still sends one compatible
chat_response, followed by chat_turn_terminal. A negotiated text_command keeps
its existing preparation and response path, with the same tracked receipts.
Completed describes processing; action_outcome does not certify a purchase,
message delivery or other external effect.

Resend the same msg_id and semantic terms after reconnect. An exact duplicate
returns its accepted/terminal receipt without rerunning preparation, the model
or tools. Replayed terminal.final_text is the historical text; original source
cards and somatic metadata are not regenerated. The client must reconcile the
existing bubble by request/session/server-turn identity, rather than append a
second bubble. reply_to and delivery mode are presentation hints, not execution
identity. Changing input/target/context/attachments or entry kind conflicts.
Another authenticated device cannot reuse or read the receipt under the same
session/request ID. Local profile operator inspection remains available.

## Private persistence and recovery

An additive nullable source_principal_json column accompanies the existing
chat_turn_receipts row. It is absent from public receipt JSON, model context
and federated memory. Named inserts preserve compatibility with the migration.
Old NULL rows are not backfilled from session membership or supplied identity.

Active ownership remains connection-bound. Legacy paired chat/text also retain
the credential callback through the existing dispatch lease; revocation, rotation,
expiry and store replacement prevent new tool dispatch. Legacy shared-key text
retains its previous scheduling contract and does not gain a device principal.
Session work uses the existing
bounded lane; control intake continues while the model/task waits. Device,
connection, store and runtime invalidation prevent new dispatch even when a
collaborator catches cancellation. Lost execution outcomes remain unknown;
restart recovery does not rerun them. New inputs are bounded per owner/session
and profile, with exact duplicate reconciliation available at capacity.

Initial verification still checks the credential secret. Subsequent dispatch
checks perform an indexed read of its device, credential kind and expiry; they
do not hash again, update last_seen or extend TTL. Expiry requires existing
credential renewal/reconnect. Retained phone-bearer records cannot authenticate
after their device is removed. Rotation/revocation/expiry during password
verification are rechecked before authentication commits its TTL update.

## Remaining authority and product work

Device tool-review listing/resolution remains unavailable. Mac operator reviews
continue through the existing central path. Private device provenance on each
TaskFlow and exact review, origin-private review publication and matching-device
resolution are the next dependency. Existing review broadcasts are not claimed
private by this receipt wave. No phone bearer should be replaced by a profile
operator credential to bypass that boundary.

Job-scoped Chrome use requires coordinated permission in ToolRunner, selected
CDP and approval resource projection while preserving the physical tab owner.
Shared conversation selection does not grant it. Optional post-turn phone skill
discovery also remains a distinct follow-up; it does not grant execution.

The iOS agent must implement stable request persistence and receipt reconciliation
before negotiating this contract. Physical LAN/ATS/pairing, the installed phone
endpoint/error and an explicitly selected provisioned inference backend are still
needed for device-to-Mac task acceptance. No real account, messages, purchases,
microphone session or model download is performed by these fixtures.

## Verification record

Final frozen acceptance passes 4,967 tests, 22 skips across 251 backend suites
(192.66 seconds). Core source hashes remain unchanged during verification.
This includes 24 new tracked-route cases, 11 legacy paired-revocation cases,
seven real credential-currentness cases and private receipt/migration tests.
Real DevicePairingStore credentials, registered HUP routes, SQLite persistence
and actual ToolRunner boundaries are exercised with inert external effects.

Independent checks also exercise two competing SQLite stores and credential
invalidation during an awaited receipt read. Six registered-route probes verify
zero new dispatch after cancellation-suppressing collaborators lose credential,
store, runtime, connection or intake ownership; unrelated operator dispatch
remains available. Earlier source-dependent results remain historical.

Full Mac mypy 1.20.2 adds or removes zero diagnostics against the existing
796-error baseline. Full-core CI Ruff, version coherence and HUP naming pass.
A return-type annotation discrepancy found in the first typing run was corrected
before the final frozen tests; the first failure is retained.

In a disposable Argon2-backed store, the former repeated verification took about
138 ms median over six samples per credential kind. Read-only currentness took
about 0.14 ms median over 200 samples per kind, without password verification or
TTL writes. These are local fixture measurements, not a general device latency
guarantee. Exact package identity and lifecycle acceptance are recorded separately
in [9.50 acceptance](NATIVE_9_50_ACCEPTANCE.md); physical phone/inference remains open.
No physical-device or inference outcome is claimed by these tests.
