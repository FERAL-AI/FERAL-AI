# Trusted context ingress integration

October2,2026. Working integration following committed634595c7194aedaf6eb9b8c91cbacb87fedea4bd.
The immutable9.29 candidate does not contain these later changes. This report
separates actual registered-route/SQLite tests with controlled providers from
real-model native acceptance in [the9.29 report](NATIVE_9_29_ACCEPTANCE.md).

## Behavior

The mandatory Orchestrator boot block installs the existing runtime checkpoint
coordinator before channel startup or primary JSON hydration. Truly unknown
legacy sessions retain their existing behavior. Existing ledger identities,
including unresolved/refused ones, cannot opt out through a missing query flag.
A failed primary ledger lookup blocks plaintext snapshot fallback; it does not
convert an unavailable checkpoint into an empty legacy thread.

An authenticated `/v1/session` can explicitly request
`context_checkpoint_version=1`. Exact supplied IDs are validated without trimming
or substituting the primary thread. Duplicate query keys and unsupported versions
are refused before attachment. The server reserves an exact opaque attachment,
initializes only trusted empty context, and reports live readiness before a
client can presave its first request marker. Existing UI messages are never
imported as trusted runtime history.

Preparation and one command share the existing SID lock in the same asyncio task.
Tracked completion waits for checkpoint commit. Phone `chat_request` and legacy
`text_command` preparation use that same scope; an unverified post-command commit
returns a redacted failure rather than a successful reply or retry instruction.
Read-only exact turn-status lookup remains available for unresolved context.

Disconnect has one retained cleanup task even after setup failure or cancellation.
It drains/cancels owned turns and waits for retained receipt settlements. Replaced
sockets cannot remove a new socket's route, audio or perception. Refcount failure
is unknown, not zero. The coordinator rechecks attachments/writers under the SID
lock before clearing volatile context; cleanup timeout retains the exact owner
rather than performing an unlocked reset. Setup/transport failure now explicitly
closes the WebSocket after recording a redacted diagnostic.

## Checks run

Parent first registered WS fixture run:14passed/3failed. Two fixture expectations
were wrong (nonexistent stream_start frame and legacy readiness spelling); a
real missing setup-failure socket close and refused-token readiness downgrade
were repaired without weakening the relevant assertions. Intermediate corrected
run:15passed/2failed, then72passed/1failed across ingress/phone/parity suites while
the worker closed the sticky readiness issue. Corrected initial17 ingress cases
passed6warnings in1.37s.

Phone integration initially failed because the older mock pairing store invented
a valid phone-bearer MagicMock; explicitly configuring no phone bearer let the
real shared-key route run without fabricating an identity. A temporary observer
was removed after locating this fixture defect. The added phone tests use the
actual registered node route, actual coordinator/SQLite, actual Orchestrator and
a controlled provider. They verify successful commit before reply, post-effect
commit failure leaves pending, and already-pending refusal before working-memory
preparation/provider execution.

Latest parent run: **50passed/18warnings in3.64s** across the23 ingress cases,
primary snapshot/trailing-edge, boot wiring and bootstrap lifecycle regressions:

```sh
cd feral-core
env FERAL_HOME=/private/tmp/feral-context-ingress-tests FERAL_DATA_HOME=/private/tmp/feral-context-ingress-tests ../.venv/bin/python -m pytest tests/test_runtime_context_ingress.py tests/test_session_snapshot_trailing_edge.py tests/test_a10_boot_wiring.py tests/test_agent_bootstrap_lifecycle.py -q --no-cov -p no:randomly --timeout=30
```

A proper package-named mypy comparison uses committed server source via
`--shadow-file` and normalizes line numbers without rewriting the baseline.
The two attributable return/coroutine diagnostics from remote848 and a prior
Awaitable/create_task diagnostic are removed. Two inferred legacy SID assignment
issues exposed by exact identity typing were corrected with the existing empty
fallback semantics. New helper's pending-task set now has an explicit annotation.
Final parent package comparison: committed server18errors vs current server/helper15;
normalized introduced[] and three removed diagnostics. The helper is clean.
This is narrow macOS comparison evidence, not the global Ubuntu type ratchet.
Latest25 ingress cases passed18warnings in2.28s, including actual gateway legacy
fallback refusal and voice start refused before mode/provider changes. The full
required frozen core regression subsequently ended in a coverage/teardown
INTERNALERROR, with all 1,261 manifest hashes unchanged. It provides no valid
full coverage result. A separate 27-suite combined run passed **597 tests, 19
warnings in 19.49s**. Later, malformed approval-owner inputs (false, zero, empty
containers/strings and invalid IDs) were refused before resolver execution, and
the minimal primary-transcript fixture bound the real new guard. The ensuing
primary/ingress/approval slice passed **46 tests, 18 warnings in 2.20s**. Evidence
logs are `/private/tmp/feral-wave4-targeted-corrected-20261002.log` and
`/private/tmp/feral-wave4-owner-repair-20261002.log`. These are focused integration
results; the required full exact-source job remains a separate gate.

## Remaining gates

Managed voice/handoff/manual mutation paths are explicitly unavailable in this
slice. Native opt-in and strict current-fence handling are separate working code;
existing threads are not silently migrated. Interrupted pending context has no
reset/replay shortcut. Explicit recovery, legacy migration/retirement, shared
lifetime memory, whole-transcript CAS, signed encrypted storage, physical glasses,
account/provider voice and clean-machine distribution remain separate cards.
The fixture providers do not establish physical-device, payment or live-account
outcomes. Full frozen integration/publication must follow all worker closures.
