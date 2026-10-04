# Managed chained voice integration

Updated October3,2026. These are source and isolated fixture results. They do not
establish microphone, speaker, cloud-account or distribution acceptance.

## Implemented behavior

The existing chained voice engine now accepts one explicit, managed utterance.
The WebSocket adapter submits through the existing tracked-turn manager and
attachment-bound context coordinator; it does not create another task or memory
authority. Configuration requires negotiated version1, chained mode and a
reviewed context checkpoint. It does not admit microphone capture.

The native client saves its request and independently reads it back before
Begin. Only the exact collecting acknowledgement admits PCM. Audio is ordered
PCM16 mono at24000Hz. Finish drains already-admitted capture callbacks, then
requests submission. Its acknowledgement is processing state, not task
acceptance; it carries no invented committed checkpoint.

Stop retires capture before awaiting cancellation and targets the original task
identity. Speech interruption only stops playback. Reconnection and status
reconciliation never automatically speak historical results or replay actions.
Negative acknowledgements preserve valid request identity without granting retry
authority. A missing terminal notification expires the adapter wait after300s
and requests reconciliation; it does not claim cancellation or retry an effect.

Only an exact live, unreplayed completed/approval/refused committed-turn receipt
can authorize speech. Full current attachment/context identity is checked before
and after awaited media writes. Context drift or a competing writer stops further
media; previously delivered audio cannot be withdrawn. Native context refresh
does not mistake an old cached READY checkpoint for a newly verified checkpoint.

Buffered managed STT supports `openai_whisper`, `groq_whisper`, `whispercpp` and
`faster_whisper`. Deepgram's existing no-op flush cannot certify this utterance
contract and is refused. Unmanaged legacy voice remains available through its
existing contract. Realtime voice is not represented as managed chained voice.

## Verification

- Parent integration:471 tests passed across20 backend suites. All1290 Python
  input hashes remained unchanged during tests, full typing and lint.
- Full nonincremental mypy:809 existing diagnostics, compared with the retained
  812-diagnostic baseline; zero added and three removed. This is not a clean
  repository-wide typecheck. The exact CI Ruff rules passed across the core.
- Engine and adapter fixtures cover admission/Stop races, strict protocol types,
  legacy compatibility, stale checkpoints, missing receipts and context changes
  during awaited TTS sends. Native checks additionally exercise save/readback,
  Begin/Finish, negative acknowledgements, drain cancellation and speech fencing.
- The archive Process fixture retains its original28 assertions and30s deadline.
  Its loopback-only generated HTTP server now bypasses unused reverse DNS and
  records bind/listen phases. Local checks passed28 with both server instances
  and clean child shutdown. Remote startup root cause remains unproven; this is
  a fixture repair, not a production startup correction.

The native final linked-run result and exact-source packaged acceptance belong
in the current checkpoint after completion. Earlier9.34 is unchanged and does
not contain these source changes. Raw captures, credentials and disposable
runtime profiles are excluded from source control.

## Remaining gates

Build and source-match the next Mac candidate, then exercise actual chat and
voice journeys with isolated data. Actual audio permission/device behavior,
provider availability, long-running work while conversing, natural interruption
and recovery need separate acceptance. This bounded utterance implementation
does not claim a complete duplex conversation experience. Reliable local-model
chat remains open after the observed upstream empty response.
