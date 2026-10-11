# Native 9.30 isolated acceptance

Recorded October 2, 2026 local time / October 3 UTC. **Actual GUI acceptance is blocked and has not run. Separate packaged headless memory and cancellation journeys passed.** Computer Use cannot initialize its native control pipe. The native host was not launched; this tool startup failure is not an observed application crash.

## Exact candidate and isolated preparation

| Field | Identity / observed result |
| --- | --- |
| Version / build | `2026.9.30` / `2026100204` |
| Runtime source | `dd69c7bf5175517966a363591fa8137782358535` |
| Executable SHA256 | `421d9756b42b3fd55a88102c396994112da561841b49e07171fe58d3aaf915b4` |
| Candidate | `desktop-native/build/FERAL Native Preview.app` |
| Coordinator manifest | `/private/tmp/feral-candidate-9-30-manifest.json` |
| Disposable root | `/private/tmp/feral-native-populated-saved-context-20261002` |
| Profile settings | Synthetic local-only Ollama `theora-coding-ba1007eb4404ce93:latest`, `http://127.0.0.1:11436/v1`, no fallback providers; proactive/multi-agent/self-learning/vision/sync disabled |
| Actual launcher guard | Exit 0; metadata/source/executable identity and disposable-profile guards passed; 482 production-core equality files in coordinator manifest; `launched:false` |
| Disk | 6.0 GiB available immediately before acceptance preparation |

The parent separately reported strict ad-hoc signature verification and bounded bundle audit passed: 13,089 files, 265 Mach-O, 9 internal links, Python 3.11.15, SQLite 3.53.1 with FTS5, OpenCode 1.18.10, packaged Python SDK factory probe. Those are coordinator audit results, not this worker's actual GUI journey or clean-machine installation. Prior 9.29 acceptance remains immutable and does not cover new saved-context source.

The exact new root contains only `feral-home`, `user-home`, `tmp`, `project`, synthetic settings and preparation/blocker receipts. No conversation database or rich legacy rows were seeded. No personal profile or ordinary preferences were opened/reset. The existing local model alias was confirmed by a read-only Ollama tags response during preparation; no weights were downloaded and no inference ran in this candidate journey.

Executed from `ASOS`:

```sh
python3 desktop-native/acceptance/native_9_30_launcher.py --check-only \
  --expected-source dd69c7bf5175517966a363591fa8137782358535 \
  --expected-sha256 421d9756b42b3fd55a88102c396994112da561841b49e07171fe58d3aaf915b4
```

This check-only invocation reads guards; it does not launch the app or write evidence. Preparation receipts were written separately within the disposable root.

## Actual control-surface blocker

1. First `cua.getState()` returned empty apps/browsers and `Native apps: Error: Sky Computer Use native pipe startup failed`.
2. `cua.js_reset()` completed. The required fresh `cua.getState()` again returned the same native-pipe error and empty inventory. Computer Use documentation loaded, but the native control service did not initialize.

No attempt was made to select/launch the default app, change OS privacy permissions, bypass the tool with AppleScript, or start an isolated host that could not be safely interacted with and quit. No native host/child/port was created for the GUI journey. Separately owned headless backend processes are recorded below. Exact synthetic blocker evidence: `/private/tmp/feral-native-populated-saved-context-20261002/9-30-cua-startup-blocker.json`.

A subsequent fresh worker reset/inventory again returned the same error; the parent independently reproduced it from its own CUA session. No extra app selection/action was performed.

## Native GUI journey matrix

| Actual journey | Result |
| --- | --- |
| Saved-context disclosure cancel/create, initial READY and empty UI record | Not run: native control pipe blocked |
| Two real local-model turns with unique harmless fact | Not run |
| Managed voice/handoff/saved-point restrictions and passive tools | Not run |
| Legacy controls, populated rich thread switching/copy | Not run; rich legacy seed also not performed |
| Normal Quit, exact host/backend-child absence | Not run; no host was launched |
| Same-candidate reopen, same SID/generation/revision/digest | Not run |
| Authorized UI-only synthetic decoy plus actual original-fact recall | Not run; no projection mutation performed |
| Exact pending-turn Stop, in-progress refusal/status and no replay | Not run |
| Final Quit/lifeline cleanup | Not run |

## Separate actual packaged headless acceptance

With GUI control unavailable, the parent authorized a separate backend journey using the exact candidate's bundled Python and core, in a different disposable root: `/private/tmp/feral-native-9-30-headless-20261002`. This is **actual provider/backend acceptance**, not native-host, layout, menu or GUI acceptance. The GUI profile remains fresh and unseeded.

Owned helper: `desktop-native/acceptance/saved_context_headless_probe.py`, final SHA256 `8b6f2fd89cef790039ba9858bf8967dd1c5f9653f67cec697b536e31b3e9e818`. Ruff and syntax checks passed. No production source or candidate resources were edited. The helper launches bundled Python with `-B`, `PYTHONDONTWRITEBYTECODE=1`, isolated HOME/TMPDIR/data paths, null keyring, native deferred-vault behavior and cache-only/offline embeddings. Server logs have an 8 MiB watchdog limit, response reads are bounded, total journey timeout is 900 seconds, and cleanup only signals the exact owned server group. Final retained headless root is approximately 3.7 MiB. No full bundle copy, model download, credential/account login or external delivery was performed.

The actual production `api.server:untrusted_app` listener was bound to `127.0.0.1` on an ephemeral port. This deliberately uses the existing stricter listener contract: loopback is not an authentication bypass on that app. A bad first auth frame was actually rejected with WebSocket close **4001**; the synthetic valid credential then negotiated the exact managed SID and both saved-context/whole-turn contracts before any prompt. Credential values were omitted from evidence.

```sh
"desktop-native/build/FERAL Native Preview.app/Contents/Resources/python/bin/python3" -B   desktop-native/acceptance/saved_context_headless_probe.py   --expected-source dd69c7bf5175517966a363591fa8137782358535   --expected-sha256 421d9756b42b3fd55a88102c396994112da561841b49e07171fe58d3aaf915b4

# Separate one-shot pending-turn cancellation; preserves original journey evidence.
"desktop-native/build/FERAL Native Preview.app/Contents/Resources/python/bin/python3" -B   desktop-native/acceptance/saved_context_headless_probe.py   --expected-source dd69c7bf5175517966a363591fa8137782358535   --expected-sha256 421d9756b42b3fd55a88102c396994112da561841b49e07171fe58d3aaf915b4 --cancel-only
```

Both commands exited 0. Actual results:

| Backend journey | Actual observed result |
| --- | --- |
| Strict authentication / negotiated attach | Invalid auth rejected 4001; valid auth reached exact managed READY, context v1 and durable whole-turn contract |
| Initial create-only UI projection | Atomic empty record confirmed, exact SID; initial READY checkpoint had zero history rows |
| Two actual tracked local-model turns | `ACK AMBER-LANTERN-731` and `42`; matching exact SID/request/turn; completed, durable, nonreplayed, action outcome `not_asserted` |
| First shutdown | PID 50215 / port 49844 absent; SIGTERM exit -15, no force kill |
| Deliberate UI-only decoy | Backed up the exact raw synthetic UI projection and SHA256; replaced three known-fact occurrences with `COBALT-PAPER-946`; checkpoint row/fence/payload remained byte-for-byte unchanged |
| Same-candidate restart | PID 50941 / port 50013; exact same SID and checkpoint generation `395dc2e5-a3f0-4a2e-bef6-67e363ee77c1`, revision 5 and 1,496-byte payload digest unchanged before new prompt |
| Actual original-fact recall | Actual model answered `ACK AMBER-LANTERN-731`, not the UI decoy. Checkpoint advanced to revision 7 after this completed turn |
| Second shutdown | PID 50941 / listener absent; SIGTERM exit -15, no force kill |
| Actual pending-turn cancellation | Third owned server PID 52540 / port 50194. Observed IN_PROGRESS revision 8 before exact request+turn abort. Durable terminal `cancelled`, action outcome `unknown`, nonreplayed |
| Cancelled context / status / next-task guard | Checkpoint stayed IN_PROGRESS, nonrestorable; capability reported unready and no READY checkpoint. Exact read-only chat.status returned the cancelled identity. A new tracked task was refused and gained no receipt; original request count remained one; no replay |
| Third shutdown | PID 52540 / listener absent; SIGTERM exit -15, no force kill |

Before shutdown/decoy/restart, the READY checkpoint had six history and four working rows, including one historical tool call/result. One **read-only** `notes_memory__list_conversations` call occurred before shutdown despite the prompt asking not to use tools. This is disclosed; prose is not a policy gate. No additional tool call appeared after restart/recall. There was no external delivery or effect-success claim. Current final headless context is deliberately IN_PROGRESS after cancellation; it was not reset or falsely promoted to ready. The original successful recall result and later cancellation result remain separate evidence.

Storage proof is narrow: the entire checkpoint row and encoded payload stayed unchanged across UI-only decoy editing and restart attachment before the new recall prompt. Its revision-5 payload SHA256 was `56f3c1f1b991b2de75d9decc6d47349f14c13d3741a919626b06caf373876bb6`. The actual answer supports this bounded runtime-context restoration; it does not certify universal life memory, long context, model obedience or every concurrent client. The synthetic display edit was an intentional fixture operation, not observed app data loss.

Evidence stays outside Git in the exact headless root: `candidate-identity.json`, `first-receipt.json`, `second-receipt.json`, `recall-receipt.json`, `final-result.json`, `ui-only-decoy-edit.json`, raw synthetic projection backup, `cancellation-result.json`, three launch/exit receipts and bounded server logs. Protocol identities are synthetic; no authentication credential is emitted. Final native executable digest remained exact and `codesign --verify --deep --strict` exited 0 after these journeys.

Diagnostic limitations: boot logs label this in-checkout bundle as an editable installation because `observability.provenance` classifies outside-site-packages paths as editable and walks up to the enclosing Git root. Independent module resolution under the identical packaged environment selected server/state/context/codec files inside the app. This resolution check is not a captured runtime stack. The separate `Self-learning: ON` banner is a hardcoded state.py string; it is not proof that the disabled feature ran. Cache-only embeddings reported the absent local embedding model and refused download; one sqlite-vec thread-affinity fallback warning was observed. No disk-caused crash or ENOSPC was observed; disk was 3.3 GiB free at final readback.

## Resume this exact acceptance

Restore the Computer Use native control pipe first. Recheck this candidate identity and profile guard, inspect for any existing owned launch receipt/process, and use the developer's guarded 9.30 launcher. Do not replace/stage the app while acceptance is active.

Seed only clearly synthetic legacy conversations in the exact new root. Observe the explicit opt-in disclosure, initial READY fence and empty UI create/readback before sending two harmless actual local-model turns. Preserve the checkpoint fence/digest and close the owned host/child. Back up the exact synthetic UI projection with a raw-byte hash receipt before the separately authorized UI-only fact-decoy edit; keep runtime checkpoint bytes/fence and other legacy records unchanged. Relaunch, compare the unchanged checkpoint before a recall prompt, and report the model's actual answer separately from storage/source proof. Never label fact recall alone as proof that UI imports are absent. Then exercise managed restrictions, pending-turn cancellation/read-only status/no replay, legacy switching, and final owned-process cleanup.

No release readiness or actual native-app stability is certified by this report. The passed bounded headless restoration/cancellation results cannot replace the pending GUI journeys. Source fixtures/typecheck are recorded separately in [saved-context source evidence](NATIVE_SAVED_CONTEXT_EVIDENCE.md).
