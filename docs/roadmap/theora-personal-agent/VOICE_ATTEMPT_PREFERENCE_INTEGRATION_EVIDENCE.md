# Voice attempt and native preference integration

October 3, 2026. These changes extend the existing voice router, authorization
coordinator, profile archive and canonical native readers. They do not create a
second task dispatcher or memory store. The implementation is currently local;
read Git for its eventual published commit. Immutable9.33 contains preceding7f
source and excludes this wave.

## Voice identity

Native Start creates a lowercase UUID paired with strict integer
`voice_attempt_version:1`. ACK, media, transcript, state and cancellation frames
must carry the exact admitted attempt. Microphone input, mute and Stop carry the
same pair. Each interrupt also carries its own request UUID; an earlier or
unbound acknowledgment cannot consume a newer pending interrupt.

Registered Start/Stop/lazy admission use the existing central SID writer lock.
A subsequent registered real-pipeline reproduction found final-audio deadlock
and speech-control starvation when media also held that lock. The repair keeps
established media and speech-only controls on captured producers without waiting
for a whole history-writing task; final audio retains its background turn and
returns after admission. Exact owned-turn cancellation precedes lifecycle writer
admission. Final combined verification below passes after that repair.
Controls verify the authenticated connection, admitted attempt and exact selected
producer before dispatch and after asynchronous boundaries. Replacement engines
cannot inherit an earlier attempt, including when a coordinator-free producer
replaces an engine without changing the connection. Foreign producers are refused
before lazy cleanup or teardown. Negotiated direct Gemini ingress also observes
the existing mute gate. Callback identity is captured at producer admission,
never retrieved from the newest SID and stamped onto an older frame.

Legacy callers omit both fields. A connection that negotiated v1 cannot downgrade
to untagged controls. Admission retains up to256 seen IDs per connection and
refuses exhaustion/reuse; disconnect retires only the matching captured owner.
Interrupt requests have their own bounded duplicate protection. These guards do
not reverse effects already dispatched or establish provider response IDs that
the provider does not supply. Managed voice remains refused until its checkpoint
integration has its own verified contract.

## Portable preferences and archive layout

The native component exports only existing canonical preferences for the exact
verified primary: companion profile, desktop behavior, selected conversation and
saved-context metadata. Imported avatars are relative references bound to size
and SHA-256; bytes travel through the existing archive. Export/apply reviews bind
primary, suite, root and snapshot, expire after300 seconds and are one-use.
Apply requires an absent destination preference domain and verified readback.
An uncertain post-publication result leaves the destination inspectable rather
than resetting or replaying it. Credentials, OS grants and Login Items
registration effects are excluded.

Compact JSON uses explicit UTF-8 key ordering and string escaping shared with
the strict Python validator. Actual cross-language tests exposed Foundation's
different numeric key ordering; nested `thread-2`/`thread-10`, Unicode/control
names and Unicode avatar references now round-trip.

The existing format1 archive accepts an explicit reviewed native attachment at
`config/.feral-native-preferences.v1.json`, with SHA/primary/root binding and
last-moment primary/avatar/snapshot checks. Restore validates the attachment and
actual archived primary before creating fresh roots. Its receipt explicitly says
`preferences_applied:false` and `runtime_started:false`.

An optional bounded `nested_roots` descriptor supports selected data beneath
config without omitted subtrees or double-counting. Restore requires matching
reviewed layout; same-root and split-root legacy contracts remain supported.
Symlinks, inverse overlap, malformed descriptors and destination/layout mismatch
are refused. Offline is a caller precondition, not a cross-process writer lease.

Actual native storage resolution still follows the backend's established
same-root `FERAL_HOME` policy. The earlier launcher declaration of
`FERAL_DATA_HOME=home/data` was not consumed by that backend. The native launcher
now resolves and declares the actual same config/data root, validates root and
ancestor metadata before creation and refuses a conflicting data override.
The existing default profile stays in place; no backend precedence or stored
data moves. Nested archive support is not evidence of native activation or of
a changed backend storage policy.

## Verified checks

Final liveness/archive integration:

- **455 passed,213 warnings,16.75 seconds** across17 related suites, including
  the actual registered gateway, real chained pipeline, real coordinator and
  disposable SQLite store. STT/TTS/command providers are synthetic; there was no
  actual microphone/provider account. Log:
  `/private/tmp/feral-integrated-liveness-archive-backend-20261003.log`.
- Four liveness regressions failed before repair: writer-lock deadlock, blocked
  speech controls, and lifecycle cancellation admission. Final audio now admits
  its retained turn promptly; mute/interrupt/subsequent media remain responsive
  while the task holds its writer scope. Exact Start/Stop cancellation, foreign
  replacement, detached/refused attachment, managed-during-read and pending
  cancellation cases pass. Legacy completion is not a durable voice receipt.
- Established legacy media uses the coordinator's public exact-token read-only
  readiness method. It verifies actual SQLite ABSENT and pre/post refusal,
  ever-managed and known-managed fences without restoration or cache writes.
  Managed-context voice stays refused. Lifecycle admission retains full readiness.
- Whole-core configured Ruff passed. Frozen full mypy retained **808 diagnostics
  in233 files/1280 checked source files**, zero added or removed versus7f.
  All1284 measured inputs stayed unchanged; digest
  `102f1cef34c100fa25eedb6424d97fc68c745df170351fcdef6747fb89d69973`.
  Baseline812 is unchanged. Evidence prefix:
  `/private/tmp/feral-integrated-liveness-frozen-mypy-20261003`.

### Earlier measurements

The following backend/type measurements precede the liveness repair. They remain
historical evidence and are not silently applied to the corrected voice sources.

- Integrated backend: **457 passed,1 skipped,281 warnings,13.72 seconds** across
  voice/provider/routes, archive/native snapshot, configuration and managed
  context refusal. Log:
  `/private/tmp/feral-attempt-preference-integrated-backend-20261003.log`.
- Whole-core configured Ruff passed. Log:
  `/private/tmp/feral-attempt-preference-integrated-ruff-20261003.log`.
- Frozen full mypy: **808 existing diagnostics/233 files/1278 checked source
  files**, zero added or removed versus preceding verified7f. All1282 measured
  inputs unchanged; digest
  `7ce9a6ddc0581e81b64379aa0abcfa5541c1a6e9cdb138d9dc25d854b5cd970d`.
  Existing baseline812 is unchanged. This is a passing ratchet, not zero errors.
  Evidence prefix:
  `/private/tmp/feral-attempt-preference-frozen-mypy-20261003`.
- Native integrated runner: **55 voice,45 voice configuration,71 preferences**,
  linked model26 groups, desktop39 and error presentation5 passed. These are
  fixture/reader checks, without actual microphone/speaker/provider use. Log:
  `/private/tmp/feral-native-attempt-preference-integrated-20261003.log`.
- Native layout follow-up:87 pure resolver assertions and linked model26,
  desktop39/error5 checks passed after BrainRuntime integration. The complete
  production app typecheck also passed. Logs:
  `/private/tmp/feral-native-profile-layout-linked-20261003.log` and
  `/private/tmp/feral-native-attempt-preference-layout-typecheck-20261003.log`.
- Actual Swift export → strict Python → standalone CLI archive/restore → actual
  Swift preference/context/avatar/journal readers plus ConfigLoader passed in a
  disposable profile. Fixture:
  `/private/tmp/feral-native-preference-attachment-cross-language-20261003-final`.
- Nested-layout follow-up:131 passed/5 warnings/2.09 seconds, including actual
  standalone CLI nested attachment. Earlier five reproduced overlap failures
  and publication-drift/canonical-ordering failures remain preserved. Log:
  `/private/tmp/feral-native-nested-archive-final2.log`.

Native Settings now wires separate backup/restore/preference reviews; controller
and actual CLI checks are in [native archive evidence](NATIVE_PROFILE_ARCHIVE_EVIDENCE.md).
The UI itself has not been observed with Computer Use.

Required new-head full CI, actual native migration UI
and profile activation, GUI/audio, signed secure setup and release distribution
remain open. No accounts, personal defaults or real payments/messages were used.
