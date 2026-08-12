# v2 mobile porting plan

> The ambient-OS design language lives in `feral-client-v2/`. This doc
> maps each web design token / primitive onto its iOS + Android equivalent
> so the three persona-critical screens (Orb, Chat, Voice) can be rebuilt
> on both platforms without drift.
>
> Scope of the initial v2 mobile drop: **tokens only**. Screen rewrites
> require a signed Xcode + working Android SDK build environment to
> verify; they are deliberately left to a follow-up commit by a dev
> running on those toolchains, per the "never claim something works until
> you've verified end-to-end" rule in `ASOS/AGENT_PROMPT.md`.

## 0. Mobile consolidation outcome (2026.5.8)

Until 2026.5.8 the tree carried four mobile codebases with three
different auth schemes and four different QR shapes. A short audit
confirmed none of them were published on any store. They are now
consolidated to one app per platform:

| Platform | Canonical path | Bundle / `applicationId` | Wire auth | QR decoder |
|---|---|---|---|---|
| iOS | `feral-nodes/ios-app/` | `ai.feral.node` *(via `$(PRODUCT_BUNDLE_IDENTIFIER)`; project-defined)* | `Authorization: Bearer <token>` over `wss://` (TLS) or `ws://` (Mode A LAN only) | `FeralBrainClient.parsePairingPayload(Data)` |
| Android | `feral-nodes/android-bridge/sample/` (uses `bridge/` library) | `ai.feral.app` *(promoted from `ai.feral.sample`)* | `Authorization: Bearer <token>` over `wss://` / `ws://` | `PairingManager.parsePayload(String)` |

Deleted in 2026.5.8 (none ever published anywhere — verified zero CI
publish workflows, no `.xcodeproj`, no signing keys, no `fastlane`):

- `apps/ios/` — `ws://?api_key=`, no QR scanner, no plist deep link.
- `apps/android/` — `ws://?api_key=`, broken cleartext config.
- `feral-nodes/android-app/` — gradle subtree incomplete; never
  constructed a WebSocket. Was a stub.

### Unified QR v1 schema accepted by both surviving apps

Both `parsePairingPayload` (Swift) and `parsePayload` (Kotlin) accept
five payload shapes and normalize them to the same struct
(`PairingDecoded { brainURL, token, brainId?, name?, isLegacy }`):

1. **Unified v1** (preferred, emitted by brains ≥ 2026.5.8):
   `{v:1, mode, url, token, brain_id, expires, name?}`.
2. Legacy `{host, port, apiKey, nodeName}` (pre-2026.5.8 iOS QR).
3. Legacy `{host, port, token, name}` (pre-2026.5.8 brain `mode=app`).
4. Deep-link URL form: `feral://pair?p=<base64url-json-payload>`.
5. Plain web URL: `https://<brain>/pair?t=<token>`.

Cases 2 and 3 log a deprecation warning and set `isLegacy = true`. Both
shapes are removed from the decoder in `2026.7.0`.

### `feral://` deep link

iOS: registered in `Info.plist` `CFBundleURLTypes` (name
`ai.feral.deeplink`, scheme `feral`, role `Viewer`).

Android: registered in `sample/src/main/AndroidManifest.xml`
`<intent-filter android:autoVerify="false">` with
`<data android:scheme="feral" android:host="pair" />` plus
`android.intent.category.{DEFAULT,BROWSABLE}`.

### Build limitation acknowledged

This commit ships the code edits + the unit tests
(`UnifiedPairPayloadTests.swift`, `PairingManagerTest.kt`). Producing
binaries — `xcodebuild` for the IPA, `gradle assembleRelease` for the
APK/AAB — requires the Xcode signing identity and Android SDK that
are NOT in the repo. Mobile binaries are built by a developer running
on those toolchains.

The Swift test target compiles for iOS only (`Package.swift` declares
`platforms: [.iOS(.v16)]`) and references UIKit / CoreImage; running
it via `swift test` from a plain macOS shell is not supported. Open
the Xcode project to execute. The Kotlin test similarly needs the
Android SDK (`testRuntimeOnly` of the JUnit + Android test runner).

## 1. Token parity (shipped 2026.5.28)

| Web token file | iOS token file | Android token file |
|---|---|---|
| [`feral-client-v2/src/styles/tokens.css`](../feral-client-v2/src/styles/tokens.css) | [`ios-app/App/FeralV2Tokens.swift`](ios-app/App/FeralV2Tokens.swift) | [`android-bridge/bridge/src/main/java/ai/feral/bridge/FeralV2Tokens.kt`](android-bridge/bridge/src/main/java/ai/feral/bridge/FeralV2Tokens.kt) |

All three files declare the same palette, type scale, radii, and motion
constants. If you update one, update all three **in the same commit** per
the systematic-sync rule in `ASOS/AGENT_PROMPT.md`.

The Android token file lives in the `bridge/` library module (not in
the `sample/` app) so any consuming Android surface — including
third-party apps embedding the FERAL SDK — picks up the same tokens
without an extra dependency. The bridge module deliberately has **no
Compose dependency**, so the file exposes raw `Int` ARGB values + `dp`
/ `sp` numbers; consumers convert in their own UI layer
(`Color(FeralV2Tokens.bgBaseArgb)` for Compose, `setBackgroundColor`
for View).

## 2. Persona-critical screens to port (follow-up work)

Only these three screens define the "one FERAL across every device"
feeling. Everything else can migrate later.

### 2.1 Ambient / Orb

- **Web:** [`feral-client-v2/src/ui/Orb.jsx`](../feral-client-v2/src/ui/Orb.jsx)
  +  [`feral-client-v2/src/shell/Ambient.jsx`](../feral-client-v2/src/shell/Ambient.jsx).
- **iOS port target:** new `Sources/FeralBridge/Views/OrbView.swift`
  (SwiftUI `Canvas` + `TimelineView` for the animation). Must read
  colors from `FeralV2Tokens.swift`.
- **Android port target:** new `ui/OrbComposable.kt` using
  `androidx.compose.foundation.Canvas` + `animateFloatAsState`.
  Tokens: `FeralV2Tokens.kt`.

### 2.2 Chat (bubble-less, Orb role indicator)

- **Web:** [`feral-client-v2/src/pages/Chat.jsx`](../feral-client-v2/src/pages/Chat.jsx).
- **iOS port target:** replace `App/ChatView.swift` so messages render
  with a small `OrbView` (size=22) as role indicator and no bubbles.
  Composer pill matches the web `.v2-chat-composer`.
- **Android port target:** add a `ChatScreen.kt` under
  `android-bridge/sample/src/main/java/ai/feral/sample/ui/` using the
  same bubble-less pattern + Compose `Surface` with
  `FeralV2Tokens.surface1Argb` + `hairlineArgb`. The legacy
  `src/main/java/ai/feral/node/ChatScreen.kt` referenced in earlier
  drafts of this doc lived inside the deleted `feral-nodes/android-app/`
  tree (see §0) and is no longer present.

### 2.3 Voice mode (visual takeover)

- **Web:** [`feral-client-v2/src/shell/VoiceOverlay.jsx`](../feral-client-v2/src/shell/VoiceOverlay.jsx)
  + [`feral-client-v2/src/hooks/useVoiceMode.js`](../feral-client-v2/src/hooks/useVoiceMode.js).
- **iOS port target:** `App/VoiceManager.swift` already exists; wrap its
  active state in a `.fullScreenCover` that shows the Orb + transcript +
  provider pill using tokens. Dim the underlying TabView to 0.4.
- **Android port target:** wrap the existing voice state in a full-screen
  `Dialog` / `ModalBottomSheet` with Orb takeover. Honest
  provider label: OpenAI Realtime / Gemini Live / Local Whisper+Piper —
  never hide which provider is driving.

## 2.4 HUP wire-protocol parity (shipped 2026.5.28)

Until v2026.5.28 the Android bridge's `TheoraBrainClient.kt` was
missing four outbound HUP message types and three inbound branches
that iOS `feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift` already
shipped. That left Android phones unable to:

- Send batched sensor frames (`sensor_batch`)
- Stream camera frames (`frame`)
- Respond to skill installation prompts (`skill_approval`)
- Confirm safety-gated tool actions (`confirmation_response`)

…and to react to the corresponding inbound prompts (`registered`,
`skill_proposal`, `confirmation_required`).

In v2026.5.28 the file was:

1. Renamed to [`FeralBrainClient.kt`](android-bridge/bridge/src/main/java/ai/feral/bridge/FeralBrainClient.kt)
   (matching iOS).
2. Moved from `bridge/src/main/java/io/feral/bridge/` to
   `bridge/src/main/java/ai/feral/bridge/` so the directory layout
   matches the `ai.feral.bridge` package declaration (the package vs
   path mismatch had been confusing IDE import organizers for months).
3. Extended with the four missing outbound methods +
   `ConfirmationResponder` callback on `FeralBrainDelegate`.

`tests/test_hup_message_parity.py` (brain repo) text-parses both
client files and fails CI if the supported HUP type sets diverge again.

## 3. Verification bar (before merging screen rewrites)

- **iOS:** `xcodebuild -scheme Feral -destination 'platform=iOS Simulator,name=iPhone 16' test` passes.
- **Android:** `./gradlew :androidTest` passes.
- Every new screen ships at least one snapshot or instrumentation test.
- Apple Human Interface Guidelines — native haptics, safe areas, no
  custom fonts beyond SF / Roboto.
- No hardcoded colors / sizes; every literal reads from
  `FeralV2Tokens.{swift,kt}`.

## 4. Why we shipped tokens-only first

See the `AGENT_PROMPT.md` "never fake / never workaround" rule. The
repo's current CI does not run Xcode or Android builds on every push
(mobile builds are dispatch-only). Shipping screen rewrites that haven't
been compiled end-to-end would be "claim something works when you
haven't verified." Tokens are pure declarations — they compile or they
don't — so they are safe to ship ahead of the screen-rewrite PRs.

The next contributor with an Xcode + Android SDK working locally
should pick up from this file, port the three persona-critical screens,
and run the verification bar above before merging.
