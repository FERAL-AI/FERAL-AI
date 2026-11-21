# FERAL Native Preview

This is an **isolated, pure SwiftUI/AppKit macOS implementation in development**. It has twenty-one partial destinations, including conversation, coding, memory, health, oversight, providers, voice, integrations, automation and app surfaces. The preview label remains until the release gates pass; it is not the intended final product name. Native appearance and a working backend do not establish feature parity or production readiness.

## Current product work

The latest verified packaged checkpoint before the current changes is **2026.9.23**.
Real checks established a bounded local-model chat reply, exact-command coding
approval, review cancellation and recovery from a short backend-probe outage.
Hardware checks used a synthetic node. Physical glasses, real speech, payments,
cloud-account setup, encrypted-profile continuation, migration and signed public
installation still require acceptance.

The full-product work and its acceptance criteria are tracked in
[release readiness](../docs/roadmap/theora-personal-agent/RELEASE_READINESS.md).
[AGENTS.md](../AGENTS.md) defines shared implementation and verification rules.
Automatic native checks are defined in
[native-checks.yml](../.github/workflows/native-checks.yml); local results and
remote CI results must be reported separately. Older results below are historical,
not the current artifact's acceptance record.

The October 1 **2026.9.24** candidate moves known model telemetry into collapsed
conversation diagnostics and usage into response details, while preserving tool
results, reviews and failures. Its transcript has an explicit available-height
layout and a bounded composer. The complete native fixture runner, web suite
(171 files / 1,358 tests), local build and strict ad-hoc signature passed. The
bounded packaged audit inspected 13,052 files, 265 Mach-O objects and 9 internal
links with no findings; isolated bundled Python FTS5/OpenCode probes passed.
Actual launch subsequently failed in both 9.23 and 9.24: macOS returned nil for
the preferences suite matching the application domain, and a force unwrap trapped
in `NativeModel.init`. The 9.25 actual launch exposed the same second trap in the desktop settings
controller. The 9.26 candidate uses existing standard defaults in both controllers for that
domain and pauses startup if a distinct selected suite cannot open. It does not
reset saved settings. The rebuilt 9.26 app launched into visible avatar onboarding,
quit normally and relaunched successfully through actual macOS interaction.
Its strict ad-hoc build and bounded 13,052-file audit passed. Desktop fixtures
passed 39 assertions, including unavailable-suite refusal without settings writes
or Login Items effects. This establishes bounded startup acceptance, not every
screen or existing-profile migration. Backend resources were staged again from
the final source; subsequent changes require renewed staging and acceptance.

The existing feature-rich web client and desktop shell remain in `feral-client-v2` and `desktop`. No web routes or Settings sections were deleted to create this preview. The installed FERAL app has not been replaced. See [FEATURE_PARITY.md](FEATURE_PARITY.md) for all 44 web route patterns and 16 Settings sections, concrete API actions, missing native controls and migration gates. [MIGRATION_PLAN.md](MIGRATION_PLAN.md) tracks the remaining full-app milestones.

Settings now includes reviewed offline backup/restore for the verified local
installation. It saves the visible conversation, stops the exact owned runtime,
and calls the existing archive CLI. Restore creates a fresh folder and preference
domain; applying the restored preferences is a separate review. It does not
activate that copy. Restarting the original profile is an explicit action.
Credentials and OS permissions are excluded. Controller, reader and real child-
process checks pass; the packaged file-picker/host journey remains unverified.
See [archive evidence](../docs/roadmap/theora-personal-agent/NATIVE_PROFILE_ARCHIVE_EVIDENCE.md).

## Isolation and first launch

- Bundle: `build/FERAL Native Preview.app`; identifier `ai.feral.native.preview`.
- Default backend data: `~/.feral-native-preview`, with data beneath its `data` directory. Default preferences suite: `ai.feral.native.preview`.
- `FERAL_HOME`, `FERAL_NATIVE_PREFS_SUITE` and `FERAL_PORT` can explicitly override test paths/port. Native config and data both follow the backend's existing `FERAL_HOME` policy. An explicit `FERAL_DATA_HOME` must match that root; a differing value is refused rather than silently ignored. Profile paths and ancestors are validated before creation. Avoid pointing test overrides at another deployment's data. There is no automatic profile activation or data relocation.
- First launch starts with avatar selection: the supplied bundled portrait, a neutral orb, or a photo imported through the native file chooser. Imported photos are copied into the preview's avatar directory. The name is optional.
- The backend is deferred until the user selects **Continue** after choosing an avatar. A previously onboarded preview starts its backend on launch. The next step configures a running local AI service and an installed model; this preview does not supply full cloud-provider/credential setup.

The sidebar and onboarding explicitly identify the app as a Native Preview.
Native voice, configuration, skills, automation, rich conversation, pairing,
hardware and integrations now have partial implementations. Consult the parity
matrix for each concrete action and remaining gap. Coding action review and
global Oversight approvals are separate queues.

The supplied FERAL logo is the product mark and app icon; the companion chosen during onboarding is a separate avatar. The exact supplied source is retained at [feral-logo-original.png](../assets/brand/feral-logo-original.png). The canonical [asset packaging script](../scripts/prepare_brand_assets.py) derives native `FeralLogo.png`/`Feral.icns`, desktop and web icon formats from that source, preserving its geometry and background. This is asset packaging, not a newly generated logo or a replacement for the user's companion image.

## Build

Current build target is Apple Silicon macOS 13+, using Xcode command-line tools/Swift and Python 3. The assembly script reuses the existing desktop pipeline's staged Python, core and OpenCode payloads. It requires `desktop/src-tauri/resources/{python,feral-core,opencode}` and the generated native assets `assets/FeralAvatar.png`, `assets/FeralLogo.png` and `assets/Feral.icns`.

From the ASOS workspace, stage current resources if needed, then build:

```sh
cd desktop
bash scripts/stage_bundle.sh
cd ../desktop-native
bash build.sh
```

`build.sh` compiles Swift, assembles the separately named app and applies/verifies an **ad-hoc** signature. It rebuilds its generated preview bundle; it does not install over `/Applications/FERAL.app`. Staging may fetch pinned dependencies when their cached resources are unavailable. Run from the intended development checkout, not as an installed-app upgrade procedure.

To launch the generated preview manually:

```sh
open "build/FERAL Native Preview.app"
```

No Apple Developer signing/notarization, distribution installer, Intel build, Linux native build, iOS build or production release approval is established by this build. The existing Linux/web work is retained; SwiftUI preview is macOS-only.

## Checks and their limits

Current native feature milestone, source/fixture coverage:

- **Health:** four tabs—Overview, Baselines, Alerts and Sources—read five APIs: `/api/health-summary`, `/api/baseline/summary`, `/api/baseline/metrics`, `/api/baseline/alerts` and `/api/dashboard`. Missing measurements stay unavailable; source heartbeats are distinct from measurement times. It does not establish physical-glasses/BLE operation, measurement freshness or clinical accuracy. **9 fixture groups passed.**
- **Memory:** the prior recent-notes reader is replaced by five sections—Recent, Search, Episodes, Execution history and Knowledge. Native controls use real stats, hybrid search/degradations, entity details and granted sharing scopes. Notes can be saved private or to an existing granted scope. Permanent saved-note deletion requires explicit confirmation and does not erase derived knowledge or historical episodes. Export preserves the loaded rows' metadata and is explicitly not a complete backup. Memory policy/backend configuration, full sync/export/import, context/wiki and episode forget/recall remain unported. **27 fixture assertions passed.**
- **Oversight:** pending global tool requests expose arguments and policy sources, with explicit approve/deny confirmation; supervisor state can be paused/resumed and events filtered. Approval grants the same tool for that session; it is not global autonomy editing. A decision receipt is not proof of tool execution success. Global policy/grants/autonomy editing, delegated Twin approvals, checkpoints and job cancellation remain unported. **10 fixture groups passed.**

These fixture tests use isolated mocked URLSession responses. They do not establish genuine hardware/provider operation or every native action.

Latest root-reported genuine checks of **preview artifact 9.9**:

- The integrated build and ad-hoc signature verification passed. The exact supplied product logo was visible on the first avatar-selection screen and sidebar. Avatar selection remains separate from the product mark. No Keychain prompt appeared in this isolated test using a null test keyring; normal production keyring behavior remains unverified.
- **Health:** actual native UI and backend responses showed current/resting heart rate and oxygen saturation as **Unavailable**, and zero baselines with no hardware samples. This verifies honest absence handling, not successful measurement capture, populated baselines, alerts, source freshness or clinical accuracy.
- **Memory:** native Save created a private disposable verification note containing “amber willow93”, tagged `native-check`. The native list showed the saved record, and cross-tier search returned Note, Entity and Knowledge rows with scores. The same isolated profile was retained after restarts. Native deletion, export, sharing to a peer, and all remaining sections have not been genuinely exercised by this check.
- **Oversight:** genuine native GUI inspection remains **unverified**. The accessibility automation connection reported a pipe-closed error after selecting Oversight, while the app stayed alive and three Oversight API polls returned HTTP 200. Restart and a plain audit-label change did not resolve the inspection failure. This is neither an observed app crash nor proof of a working native approval/pause workflow. No genuine approval, denial or supervisor action execution is claimed.

The full native migration and remaining route/settings parity are still pending. Artifact 9.9 and its bounded real checks do not establish production readiness.

Recorded earlier evidence:

- Actual macOS 13-target Swift typechecking/build and ad-hoc signature verification have passed. Rebuilds/native checks are required after subsequent source corrections; an earlier build is not proof of the latest artifact.
- Native UI has actually been launched: avatar-first selection and the native project-folder chooser were observed. An initial folder selection exposed a stale accessibility-tree label despite the correct rendered path. After the bounded identity/containment correction and restart, root observed the actual selected path `/private/tmp/theora-swiftui-preview-probe-20260930/project` in a fresh native Coding accessibility tree.
- Recompiled native model/wire regressions passed **7 groups**, exercising terminal/error/partial-stream handling, persistence, observed action deduplication, ACP multi-file/command review, structured coding failures and workspace changes. These use mocked HTTP/wire frames, **not real model or engine execution**. Log: `/private/tmp/theora-native-model-tests.log`; source: [NativeModelTests.swift](NativeModelTests.swift).
- Latest root-reported focused backend selection passed **619 tests, with 1 skipped**. Supplemental doctor checks had **2 failures** reflecting the real low-disk condition (about **3.5 GiB free**); these failures were not waived or presented as passing. Selections overlap with earlier runs and must not be added into a global total.
- Earlier backend endpoint selection passed **63 tests**, 7 warnings. Log: `/private/tmp/theora-native-endpoint-regressions.log`. These are backend regressions, not proof that every native route is ported.
- Final relocated preview payload smoke **passed**, with bundled Python/core and OpenCode **1.18.10**, identifier `ai.feral.native.preview`, and original/relocated signature integrity before and after backend boot. This used a test-only null keyring, provider `none` and hash embeddings; it did not exercise GUI lifecycle or Linux. Log: `/private/tmp/theora-swiftui-final-payload-smoke-20260930.json`. Earlier payload identity evidence is superseded by this final preview-artifact check.
- Real child-process lifeline tests reran: **2 passed**. These exercise backend process-group termination when the app lifeline closes and continued operation while the lifeline remains open.
- The retained web client's full suite passed 170 files / 1,352 tests before later bounded changes. That verifies the web client, not native feature parity.

Latest root-reported real SwiftUI check: the native user sent “Reply with exactly: silver fern 82” and the actual accessibility tree showed the assistant reply “silver fern 82”. The test used the local service at port 11435, a single-agent test configuration and a null test keyring, with no paid provider. Existing user-thread content and the selected avatar restored after restart. This establishes **one literal local-model reply**, not general chat quality or production keyring behavior.

Genuine native coding checks then used the local service at port 11435 and a prepared alias of `qwen2.5:3b`, in an explicitly selected temporary project:

- **Allowed write passed:** actual native Before/After/diff review showed `native-check.txt`, empty Before and proposed `silver fern 82` After. After clicking Allow, the observed write completed and a separate file read confirmed exactly `silver fern 82\n`.
- **Denied write passed:** a proposed replacement with `DENIED CHANGE` appeared in the actual native review. After clicking Deny, the observed write failed and the file remained exactly `silver fern 82\n`. The engine task reached Completed, meaning the engine finished; this did **not** mean the denied edit executed.
- No shell/test commands were executed by these coding checks. They establish a bounded file-write approval/denial workflow, not arbitrary coding quality, command execution or full native parity.

After genuine chat, allowed write, denied write and Quit, root confirmed no preview native host, owned backend launcher or owned OpenCode ACP processes remained. App signature verification after those actions passed. These genuine UI checks are distinct from the fixture-only model tests and the non-GUI payload smoke.

Broader native conversation/coding quality, all missing features and full parity remain unverified. No clinical sensor accuracy, physical-glasses/BLE session, payments or public relay deployment is established here.

The credential vault now guards unavailable macOS default-keychain state before attempting to store its master key; it fails explicitly instead of creating/resetting a keychain. The exact cause of the earlier observed Keychain dialog remains **unproven**. The latest isolated preview launch was reported without that dialog; this is not a guarantee that an existing Keychain will never ask for legitimate access. Backend startup is deferred before first avatar selection.

## Local regression commands

From `desktop-native`, use the maintained runner, which includes every linked
source required by the current model. These are fixture/contract checks, not
genuine provider or physical-device acceptance:

```sh
bash test_features.sh
python3 -m unittest -v test_native_lifeline.py
```

The lifecycle tests exercise real local child-process cleanup, not model correctness. The preview's launcher uses a separate backend process group and an app lifeline; tests and actual quit/relaunch checks should continue to verify no owned backend is orphaned. Do not terminate unrelated FERAL instances to make a preview test pass.

See [desktop evidence](../docs/roadmap/theora-personal-agent/VERIFIED_DESKTOP.md) and [verification report](../docs/roadmap/theora-personal-agent/VERIFICATION_REPORT.md). Historical evidence remains dated; [the parity matrix](FEATURE_PARITY.md) governs native completeness. Keep the full app available until every retained capability has an explicitly reviewed native implementation or an agreed access path.
