# FERAL desktop verification and bounded shortcut fix

Publication note: checkout paths are portable placeholders. Set `EVIDENCE_ROOT` to a new disposable directory outside personal/app data before running the commands below, for example `export EVIDENCE_ROOT="$(mktemp -d)"`. Evidence filenames identify historical local outputs, not shipped archives or fresh reruns. `<theora-ios-checkout>` denotes the separate Theora iOS repository.

## Current desktop status — supersedes readiness interpretations

The new pure SwiftUI app is **FERAL Native Preview**, a separately named/identified/data-isolated prototype with **seven partial screens and no full native feature parity**. It must not replace the retained full-feature FERAL app. No existing web feature was deleted for the preview, and the installed app remains unchanged. Its defaults are `~/.feral-native-preview` and preferences/bundle identifier `ai.feral.native.preview`.

Read the [preview README](../../../desktop-native/README.md) and [complete parity inventory](../../../desktop-native/FEATURE_PARITY.md): 44 web route patterns and 16 Settings sections remain the baseline. Native Health, expanded Memory and global Oversight now have implementations, with fixture evidence of 9 Health groups, 27 Memory assertions and 10 Oversight groups. Artifact 9.9 build/signature checks passed, and the supplied product logo was observed on first avatar onboarding and the sidebar. Genuine Health UI showed unavailable heart-rate/SpO2 values and zero baselines consistent with APIs without hardware samples. Genuine Memory private save/list/cross-tier search passed, and the isolated profile was retained after restarts; native deletion/export/peer sharing and other section walks remain unverified. Oversight native GUI inspection remains unverified after an accessibility pipe-closed failure, despite app liveness and three HTTP 200 API polls; restart/audit-label changes did not resolve inspection. No genuine Oversight approval/denial/pause execution is claimed. No Keychain prompt appeared in the isolated null-test-keyring launch; production keyring behavior is not established. Voice, policy/Grants editing, Skills, automation, rich conversations, pairing/hardware, integrations and most settings remain unported. Passing historical Tauri/web/backend checks below does not prove native completeness or production readiness. One root-reported actual SwiftUI local-model literal reply was observed (“silver fern 82”), with thread/avatar restoration after restart and signature verification after startup. Root also verified one native permission-approved file creation and one denied replacement that preserved the file. This is narrow chat/file-action evidence, not general quality or full feature parity. No notarization, Linux-native or iOS release claim follows.

The [full migration plan](../../../desktop-native/MIGRATION_PLAN.md) remains the completion gate. The supplied logo is retained as the canonical product mark and packaged into icon formats; the selected companion avatar is separate. Current feature implementations do not establish full parity, clinical validity or release readiness.

The vault's missing-default-Keychain guard and deferred backend startup were corrected, but the exact cause of the earlier Keychain dialog remains unproven. Keep prior test results as dated evidence, not permission to swap the user's app or migrate their data. New native evidence must identify the exact preview artifact and distinguish mocked wire tests from real execution. Latest root evidence: selected project path appears correctly in a fresh Coding accessibility tree after restart; focused backend selection passed 619 tests with 1 skipped. Supplemental doctor checks still had 2 real low-disk failures (about 3.5 GiB free). Final relocated preview payload/signatures passed with bundled Python/OpenCode 1.18.10 (test-only null keyring/provider none/hash). Native allowed write created exactly `silver fern 82\n`; denied replacement left those bytes unchanged, with an observed failed write despite the engine finishing. No coding shell/test commands were run. Two real lifeline tests passed; after final Quit, no owned preview host/backend/OpenCode ACP process remained and signature verification passed. See the README for scoped details.

2026-09-30. Workspace: `<checkout>/ASOS`. No iOS changes/builds, OS permission changes, dependency installs, real model/provider requests or real payment actions. Initial verification was read-only; the parent subsequently authorized the bounded desktop shortcut fix and canonical token regeneration.

## Initial evidence

- `desktop/src/main.js` installed a native `voice-activation` listener but dispatched a DOM custom event on the shell window. The brain runs in a separate iframe. No `feral-voice-activation` receiver existed in the web-client source.
- A local Node/jsdom harness extracted and executed the actual original `showBrain` function and the actual dispatch line from `desktop/src/main.js`. Parent listener received one event; iframe listener received zero. Actual frame permission policy only delegated clipboard. This experimentally confirms the DOM boundary failure, without claiming native hotkey or microphone testing.
- `npm run check:tokens` in desktop exited 1: committed generated `src/tokens.css` stale against canonical `feral-client-v2/src/styles/tokens.css`.
- Node/npm and desktop/client dependencies were already installed. Rust/cargo were installed under `~/.cargo/bin` but absent from the shell PATH. Existing desktop dist and staged brain/Python resources were present.

## Native compile result

Actual commands:

```sh
rustup toolchain list
cargo --version
rustc --version
cd desktop/src-tauri
cargo check --offline --locked --target-dir ${EVIDENCE_ROOT:?}/theora-desktop-rust-target-20260930
```

Active toolchain: stable-aarch64-apple-darwin, cargo/rustc 1.96.0. Native macOS check passed, exit 0, in 1 minute 24 seconds. Cached dependencies compiled without network; generated compilation artifacts were directed to tmp. This is a type/compile check, not a linked production app, notarized bundle or Linux build. Cargo identifies the crate as 1.0.0 although desktop npm package is 2026.9.7; existing metadata mismatch was not altered.

## Bounded changes

- `desktop/src/voiceShortcut.js`: exported real native-event registration handler and narrowly scoped forwarding. It sends only `{type: 'feral-desktop-voice-activation', v: 1}` to a connected loopback HTTP(S) iframe's exact origin; absent, detached or external frames receive nothing.
- `desktop/src/main.js`: registers that handler against Tauri events and resolves current brain frame at event time; iframe microphone policy delegates only to the known brain origin. This permits the ordinary user/OS-controlled capture path; it neither grants nor requests OS permission during tests.
- `feral-client-v2/src/lib/desktopVoiceShortcut.js`: receiver checks immediate parent source, native Tauri origin allowlist, exact two-field/versioned payload, and framed context. Dev server origins are only enabled in development. No wildcard origins, arbitrary commands or cross-window tool execution.
- `feral-client-v2/src/shell/VoiceContext.jsx`: registers/unregisters receiver and routes activation into ordinary `useVoiceMode.start`, keeping its existing active/disconnected/provider/permission behavior.
- `desktop/src/tokens.css`: regenerated via `npm run sync:tokens` from existing canonical tokens, without design edits. The large diff is generated drift removal.
- `feral-client-v2/src/__tests__/shell/desktop_voice_shortcut.test.jsx`: eight regressions exercising real registration/forwarding and real VoiceProvider/useVoiceMode effects, with realtime audio engine mocked so tests never access microphone/providers. Covers exact origin forwarding, disconnected status, active-session repeat, untrusted origin/source/shape/version, native origins, cleanup, standalone browser and detached/external iframe rejection.

## Checks after changes

From `desktop/`:

```sh
npm run sync:tokens
npm run check:tokens
node --check src/main.js
./node_modules/.bin/vite build --outDir ${EVIDENCE_ROOT:?}/theora-desktop-dist-20260930
```

All passed after regeneration; build emits both main and floating-window HTML. Build was repeated after final handler extraction and passed. No staging/download/package script was run.

From `feral-client-v2/`:

```sh
./node_modules/.bin/vitest run src/__tests__/shell/desktop_voice_shortcut.test.jsx src/__tests__/shell/voice_wire_fields.test.jsx src/__tests__/hooks/useVoiceMode.test.jsx src/__tests__/hooks/voice_thread_switch.test.jsx
./node_modules/.bin/vite build --outDir ${EVIDENCE_ROOT:?}/theora-client-dist-20260930
```

Four test files and 37 tests passed, including 8 new shortcut tests. Client production build passed. Test output includes existing Node localstorage-file warnings. Client build warns about existing >500KB chunks and mixed static/dynamic useFeralSocket imports. Neither warning caused failure. `git diff --check` passed.

## Practical limits and next evidence

No installed desktop app was launched, no global key was pressed, no browser permission prompt was accepted, and no live audio was captured. The meaningful automated result is shell callback → bounded message → real client voice-start state/engine invocation; audio transport is intentionally mocked. Native compiled Rust was unchanged by this fix. To exercise the fix in a bundled app, rebuild/stage both the desktop shell and brain-served client; the tmp JS output does not update an existing installed application or staged dashboard. Native macOS and Linux smoke tests should confirm Tauri's actual sender origin, hotkey registration, iframe microphone policy, ordinary permission denial/recovery, and audible interruptible response with an explicitly configured test provider.

The current main-window CSP limits frames to loopback HTTP, so external HTTPS hosted-brain support is not implied. Receiver rejects opaque/null origins deliberately; if a target WebView supplies an opaque sender origin, add a verified enrollment/bridge design rather than accepting all null senders.

## Consumer desktop shell and final client regression

The consumer shell now opens Chat at `/`, keeps Home at `/home`, and exposes Chat, Coding, Memory, Devices, Needs you, Settings, and More as the seven labelled navigation controls. More and the keyboard palette retain all advanced destinations. Coding uses the actual project/task page. Activity sidebar defaults closed and remembers user preference; status details remain in an accessible Activity menu. Narrow layouts overlay the sidebar, keep navigation controls at least44px, and avoid document overflow. Skip-to-content has a visible focus ring. Both standalone and shell Chat greet with “How can I help?”; the conversation title uses free header space without losing narrow-screen ellipsis.

Final integrated verification from `feral-client-v2/`:

```sh
./node_modules/.bin/vitest run --maxWorkers=1
```

**170 test files and 1,352 tests passed**, exit0, in276.30s on2026-09-30. Default test timeouts were preserved; no other UI runs occurred concurrently. Log: `${EVIDENCE_ROOT:?}/theora-full-client-final-single-worker.log`. Earlier concurrent runs hit host-pressure timeouts and greeting fixture drift; the fixture updates and focus correction are included in this successful full run.

Client production build and `git diff --check` pass. The package has no lint script/config; no lint pass is claimed. Existing build chunk/import warnings and Node localStorage warnings remain non-failing.

Browser shell/status suite passed21of22initial checks; the depth2 hard-load check timed out while the served preview dist was rebuilt. Both depth1 and `/memory/context` hard-load checks then passed against the stable build. Coverage includes all advanced navigation, keyboard palette, composer overlap, sidebar keyboard/typing safety, status controls/popovers, and horizontal overflow at1280/375. Screenshot harness additionally verifies all seven navigation targets fit at820/390/360 with minimum44px controls. These API-mocked browser checks establish layout/interactions and do not claim successful provider/task execution.

Visual artifacts: `${EVIDENCE_ROOT:?}/theora-shell-before.png`, `${EVIDENCE_ROOT:?}/theora-shell-after.png`, `${EVIDENCE_ROOT:?}/theora-shell-more.png`, `${EVIDENCE_ROOT:?}/theora-shell-activity.png`, and responsive `${EVIDENCE_ROOT:?}/theora-shell-{820,390,360}.png`. Final desktop screenshot shows the full “New conversation” title. Parent owns packaged WKWebView/native smoke and final package rebuild; no native runtime or OS permission claim follows from these browser screenshots.

## Native onboarding copy follow-up

After the parent observed the actual packaged `/setup` screen, a bounded copy-only cleanup replaced CLI/config-storage language with consumer instructions. Tabs now read Welcome, AI provider, Voice, About you, Connect devices, Ready. Welcome explains local/cloud AI, optional voice/personal details, device connection, and later changes in Settings. Provider/voice labels avoid environment variables, installation commands, vault internals, and STT/TTS abbreviations. Finish now describes starting chat rather than writing files. Pairing URLs and useful network diagnostics remain available. No setup behavior, endpoints, state, permissions, or layout changed.

Focused existing Setup/pairing regressions: **2files,7tests passed**, default timeouts,2.94s (`${EVIDENCE_ROOT:?}/theora-setup-consumer-copy-tests-final.log`). Production client build passed4.70s (`${EVIDENCE_ROOT:?}/theora-setup-consumer-copy-build.log`) and `git diff --check` passed. These copy edits occurred after the full1,352-test pass and require the final package rebuild.

## Native folder bridge follow-up

Parent observed the actual Coding Choose folder action showing an instruction but opening no native dialog. Source inspection found the client request trusted `document.referrer` as the parent target origin, with a wildcard fallback; a loopback referrer addresses the wrong parent and is silently dropped. The observed WebView referrer/event origin was not inspected, so this is a confirmed source flaw and plausible explanation, not a claimed live cause measurement.

The bounded fix routes requests only to the explicit native shell origin allowlist already used by voice activation, independent of referrer and without a wildcard. Browser origin matching delivers only to the matching immediate parent. Replies now require trusted native origin, immediate-parent source, matching outstanding request ID, and exact result schema. Native shell continues to reject any sender except its exact loopback brain iframe origin/window. Folder selection still does not grant access; the separate Grant project folder action remains required. No Rust/native permission changes.

Actual Coding component→actual shell folder handler→mocked native invocation→client project-field update regression passes with a misleading loopback referrer. New strict reply-origin/source/schema/stale-ID and standalone/unsupported-target checks also pass:3tests,1.44s test time (`${EVIDENCE_ROOT:?}/theora-folder-picker-isolated-tests.log`;15s diagnostic limit after initial5s timeout under concurrent host pressure, no permanent timeout change). Existing Coding/voice16tests passed. Desktop bridge2tests, token parity, JS syntax, client production build35.46s, and diff checks pass. Root owns actual rebuilt WKWebView confirmation; automated native invocation is mocked and does not itself prove a live dialog opens.

## Complete backend regression after desktop implementation

The isolated complete backend run passed **11,687 tests, 50 skipped, 538 warnings**, exit0, in693.25s. Log: `${EVIDENCE_ROOT:?}/theora-desktop-core-20260930.log`. It used cached local embeddings with offline hub settings and permitted local sockets. See [the complete verification report](VERIFICATION_REPORT.md) for the exact command and failure investigation scope. This is backend regression evidence, separate from native window/provider/folder-picker tests and assembled artifact verification.

The final Setup copy and OpenCode environment override changes require a fresh assembled artifact. Earlier packaged smoke/signature reports describe their tested artifact; final rebuilt-app relocation and metadata verification are pending the build completion signal. No iOS, clinical, physical glasses/BLE or Linux result is implied by the backend pass.
