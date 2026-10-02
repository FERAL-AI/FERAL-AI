# Direct verification and bounded corrections

Publication note: checkout paths are portable placeholders. Set `EVIDENCE_ROOT` to a new disposable directory outside personal/app data before running the commands below, for example `export EVIDENCE_ROOT="$(mktemp -d)"`. Evidence filenames identify historical local outputs, not shipped archives or fresh reruns. `<theora-ios-checkout>` denotes the separate Theora iOS repository.

## Current desktop scope — native preview is not a replacement

The retained feature-rich FERAL web/desktop app remains available and the installed app has not been replaced. The newly built **FERAL Native Preview** is isolated under `ai.feral.native.preview` with default data `~/.feral-native-preview`; its seven native screens are all **partial**, not a full port. See the [preview README](../../../desktop-native/README.md) and [feature parity map](../../../desktop-native/FEATURE_PARITY.md) before interpreting any desktop result below.

Historical source/unit/browser/backend/Tauri package evidence does not establish native feature parity or production readiness. Native Health, expanded Memory and global Oversight now have implementations, with fixture evidence of 9 Health groups, 27 Memory assertions and 10 Oversight groups. Artifact 9.9 build/signature checks passed, and the supplied product logo was observed on first avatar onboarding and the sidebar. Genuine Health UI showed unavailable heart-rate/SpO2 values and zero baselines consistent with APIs without hardware samples. Genuine Memory private save/list/cross-tier search passed, and the isolated profile was retained after restarts; native deletion/export/peer sharing and other section walks remain unverified. Oversight native GUI inspection remains unverified after an accessibility pipe-closed failure, despite app liveness and three HTTP 200 API polls; restart/audit-label changes did not resolve inspection. No genuine Oversight approval/denial/pause execution is claimed. No Keychain prompt appeared in the isolated null-test-keyring launch; production keyring behavior is not established. Voice, global policy/grant editing, skills/automation, rich conversations, device pairing/hardware, integrations and most settings remain missing. Root observed one actual native SwiftUI local-model literal reply (“silver fern 82”), thread/avatar restoration after restart, and signature verification after startup. This proves only that narrow native chat interaction. Native coding then verified an allowed file creation and a denied replacement that preserved the original bytes. Broader quality and full feature parity remain unverified; mocked wire tests are separate evidence. No preview replacement, automatic data migration, notarization, Linux-native or iOS production readiness is claimed.

The [full migration plan](../../../desktop-native/MIGRATION_PLAN.md) remains the completion gate. The supplied logo is retained as the canonical product mark and packaged into icon formats; the selected companion avatar is separate. Current feature implementations do not establish full parity, clinical validity or release readiness.

The missing-default-Keychain guard and avatar-before-backend startup were corrected; the earlier dialog's exact cause remains unproven. Preserve the full app and data while completing the parity matrix. The dated results below continue to support only their stated corrected behaviors and test scopes. Latest root evidence records correct native Coding project-path accessibility after restart and a focused backend selection of 619 passed, 1 skipped; supplemental doctor checks had 2 actual low-disk failures (about 3.5 GiB free). Final relocated preview payload and original/relocated before/after-boot signatures passed with bundled Python/OpenCode 1.18.10 under test-only null keyring/provider none/hash. Genuine native Allow created `native-check.txt` with exactly `silver fern 82\n`; Deny prevented replacement with `DENIED CHANGE` and preserved the bytes. The denied write was observed failed even though the engine task finished. No coding shell/test commands were executed. Two real lifeline tests passed. Final Quit left no owned preview host/backend/OpenCode ACP processes and the app signature still verified.

September 30, 2026. Scope: FERAL core, browser client and desktop. Theora iOS implementation/builds remain with its separate agent. Baseline ASOS commit: `39207375526974ea8bf398899cede444e19c9ac9`; corrections are uncommitted working-tree changes.

## What was reproduced and corrected

| Finding | Executable evidence | Correction |
|---|---|---|
| Spending read failure became zero spending | Actual SQLite table fault changed an over-budget denial into an allowance | Unavailable-ledger decisions deny; direct reads raise an explicit error |
| Failed audit writes appeared successful | Actual SQLite faults/trigger left the record absent without an error | Failed writes raise; the actual purchase skill returns failure without a preview/card |
| Purchase card Confirm/Cancel did nothing | Generated IDs dispatched through the real UI handler produced no effect or response | Unsupported buttons removed; result explicitly identifies read-only preview and unavailable checkout |
| Scraped first price presented as a total | Synthetic browser returned $24 and $900; actual skill selected first candidate | Observed price labelled unverified; no cart/tax/shipping/checkout-total claim |
| Live HR became “Resting Heart Rate” | Actual aggregator/HUP builder duplicated a live 145-bpm reading into resting HR | Live/current HR remains separate; only qualified provider data fills resting HR |
| Native voice shortcut stayed outside the client | Actual source executed in jsdom delivered one parent event and zero iframe events | Exact versioned message, restricted target/source/origin, ordinary client voice-start path |
| Desktop tokens were stale | Canonical token parity check failed | Generated copy refreshed through existing generator |
| `places` lacked a display family | Complete client suite failed shipped-manifest coverage | Added search family mapping; full client suite passes |

No transaction engine, payment execution, new sensors or unified consumer-account platform was introduced by these corrections. Read-only product preview is the current verified purchase capability.

## Testing method

Use throwaway `FERAL_HOME` and `FERAL_DATA_HOME`, synthetic data and provider mocks for policy/unit tests. The initial broad core run forced hash embeddings; the final run uses the existing cached local model with offline hub settings. Paid embedding APIs were not selected. Chromium was downloaded into temporary storage after the sandbox blocked DNS. Real-browser tests point only to an isolated local brain with no model/provider credentials stored in its vault, peer sync disabled after test initialization, proactive behavior disabled and no real purchases.

The runtime for browser tests is a copy of tracked core code in `${EVIDENCE_ROOT:?}/theora-real-brain-verification`, with the current production client build copied into its `webui_v2`. Corrected source files were refreshed before the final browser pass. This avoids replacing the repository's staged/committed dashboard or an installed app.

## Recorded results

- Complete client suite: **168 files, 1,348 tests passed** after fixes. The prior run had one failure for `places` mapping and 1,339 passes. The unrestricted-worker attempt was interrupted; only the completed four-worker run counts.
- Production client build: passed after changes. Existing large-chunk and mixed-import warnings remain.
- Desktop JS build, syntax and generated-token parity: passed. Native macOS `cargo check --offline --locked` passed using cached dependencies and a temporary target directory. This is not a packaged-app launch or Linux build.
- Desktop voice regressions and related voice tests: **37 passed**, including eight new cases with mocked audio transport.
- Commerce/web-action/approval selection: **61 passed** after ledger and truthful-preview corrections. Three ledger fault regressions exercise actual SQLite failures.
- Health selection after HR correction: **110 passed**; later BP/health/history selection **79 passed**. These selections overlap and must not be added together.
- Semantic retrieval selection: **18 passed** with cached local embeddings and offline hub settings. A separate executable probe confirmed `fastembed`, loaded model and 384 dimensions.
- Workspace-shell selection: **18 passed** after correcting test interpreter selection.
- macOS AX selection: **62 passed, 14 grant-dependent skips** in sandbox; actual read-only Finder depth test **passed** with permitted execution. No Accessibility settings were changed.
- Real-brain destination pass on the corrected runtime: **30 passed**, covering 28 navigation destinations and 51 distinct REST paths plus actual session WebSockets and detector/route coverage checks.

- Earlier complete core run after the initial bounded source/test corrections: **11,667 passed, 50 skipped, 538 warnings**, exit 0, in 661.48 seconds. Used actual cached local embeddings with offline hub settings and permitted local sockets. No failure was waived or marked expected to obtain this result.
- Selected real-brain control walks: **five passed**, 191 clicks across chat, memory, devices, approvals and settings, in 4.3 minutes. Zero thrown errors, silent request failures or unclickable controls were reported. Disabled/destructive controls were skipped; settings hit the existing 45-click budget, so this is not every settings operation.
- The generic walk reported two “dead” chat controls because DOM/request observation does not see every browser-side effect. A separate real Chromium probe received the actual Attach-file chooser event and verified multiple-file capability without selecting/uploading a file. The Copy handler called an instrumented clipboard writer with nonempty text. Actual OS clipboard contents were not tested or changed by that separate probe.

The final suite's warnings remain visible, including existing async cleanup/thread warnings and deprecations. Passing tests do not remove that cleanup work from the release backlog.

## Desktop implementation core regression

The subsequent complete backend run passed **11,687 tests, with 50 skipped and 538 warnings**, exit 0, in **693.25 seconds**. Raw log: `${EVIDENCE_ROOT:?}/theora-desktop-core-20260930.log`. This includes the current backend test tree, isolated `FERAL_HOME`/`FERAL_DATA_HOME`, cached local embeddings with offline hub settings, and permitted local socket access. The fresh-install doctor fixture now models Tailscale responses and healthy host permissions instead of depending on the developer's live daemon or launching process; production timeout/permission warnings remain intact. No failure was waived to obtain the pass.

Reproduction from `ASOS/feral-core`:

```sh
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-desktop-core-20260930 FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-desktop-core-20260930/data FERAL_EMBED_PROVIDER=local HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ../.venv/bin/python -m pytest tests/ -q --no-cov -p no:randomly --tb=short
```

This result verifies backend regressions; it does not establish iOS, clinical accuracy, physical glasses/BLE behavior, native GUI or real model quality. Packaged app evidence is recorded separately. A new assembled artifact after the final Setup copy and OpenCode environment override changes still requires its own relocation/startup/signature verification; earlier artifact reports must not be treated as that final artifact's result.

## Broad core failure investigation

The completed initial permitted broad run produced **11,661 passed, 50 skipped, five failed, 542 warnings**, in 740.46 seconds. It is retained as a failed run, not relabelled green.

1. BP history test used `asyncio.get_event_loop()` after a previous `asyncio.run()` cleared the loop. Ordering was reproduced; conversion to pytest-managed async/await fixes the fixture, with no production BP change.
2. Live AX depth test compared a 30-second baseline with an eight-second bounded follow-up. Real isolated testing and a deterministic clock/tree test verified that timeout was a valid product outcome. Both live walks now get the same budget; runtime code is unchanged.
3. Two semantic retrieval cases failed with explicitly forced hash embeddings. The same complete retrieval file passes with actual cached local embeddings; no ranking implementation was changed to accommodate the hash fallback.
4. Workspace-shell test invoked host `python3`, which lacked pytest, although the suite ran under the pinned virtualenv. It now invokes the actual test interpreter through the real shell; no systemwide package installation or shell-policy change.

An earlier full run was interrupted because sandbox socket restrictions invalidated integration cases. One collection attempt overlapped an agent's source update and failed on the newly introduced exception import; it was discarded and rerun after stabilization.

## Reproduction commands

From `ASOS/feral-core`:

```sh
FERAL_HOME=${EVIDENCE_ROOT:?}/theora-final-core FERAL_DATA_HOME=${EVIDENCE_ROOT:?}/theora-final-core/data FERAL_EMBED_PROVIDER=local HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ../.venv/bin/python -m pytest tests/ -q --no-cov -p no:randomly --tb=short
```

From `ASOS/feral-client-v2`:

```sh
npm test -- --maxWorkers=4
npm run build
PLAYWRIGHT_BROWSERS_PATH=${EVIDENCE_ROOT:?}/theora-playwright-browsers FERAL_E2E_REAL_BRAIN=1 FERAL_E2E_URL=http://127.0.0.1:9462 ./node_modules/.bin/playwright test real_brain_pages --reporter=list
PLAYWRIGHT_BROWSERS_PATH=${EVIDENCE_ROOT:?}/theora-playwright-browsers FERAL_E2E_REAL_BRAIN=1 FERAL_E2E_URL=http://127.0.0.1:9462 ./node_modules/.bin/playwright test real_brain_controls --grep '/(chat|memory|devices|approvals|settings) \(' --reporter=list
```

Local socket/browser/AX execution required permitted access outside sandbox. The named brain must be a throwaway test instance; control walks mutate it. The browser-only destination checks do not execute every action. The full 61-case combined browser lane was interrupted after two completed control cases; only completed selected runs are reported.

Detailed evidence: [commerce](VERIFIED_COMMERCE.md), [desktop](VERIFIED_DESKTOP.md), [health](VERIFIED_HEALTH.md), [AX](VERIFIED_MACOS_AX.md). Temporary raw logs: `${EVIDENCE_ROOT:?}/theora-full-core-final.log`, `${EVIDENCE_ROOT:?}/theora-client-tests-final.log`, `${EVIDENCE_ROOT:?}/theora-real-brain-pages-final.log`, `${EVIDENCE_ROOT:?}/theora-real-brain-critical-controls.log`.

## Limits that remain explicit

No iOS build, physical-glasses session, biosensor accuracy/clinical study, live merchant/payment, production public relay, actual global-hotkey/microphone session, installed-app upgrade/notarization or Linux native execution was verified here. These require their own evidence; a mocked transport or passing compile check cannot substitute.

Existing async test cleanup warnings, deprecated APIs, large bundles and version metadata differences remain visible. Customer demand, pricing and retention still require the proposed pilot. Unified ownership/private continuity, hosted operation and full transaction completion remain planned engineering work. This verification supports the specific corrected behaviors and tested software paths, not a claim that the full proposed product is finished or release-ready.
