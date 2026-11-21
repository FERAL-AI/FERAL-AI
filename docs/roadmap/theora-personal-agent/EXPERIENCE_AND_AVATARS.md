# Proposed consumer experience and avatar system

September 30, 2026. These are product specifications to implement and validate, not current feature claims. Keep the consumer journey small enough to use every day while preserving the existing advanced runtime surfaces.

## Three journeys for the first pilot

### Remember a commitment and follow through

The user explicitly starts capture or uses an enabled wake phrase on their glasses. Recording status and a stop control remain clear on the available hardware/phone surface. After the conversation, the agent says: “You mentioned sending Maya the proposal tomorrow. Save that as a task?” The app shows the source transcript and uncertainty. The user corrects an incorrect name or accepts the task.

The pilot must also handle other participants: the wearer can explain what is captured and where it goes, respect a participant declining recording, stop immediately, and remove the session plus derived memories when appropriate. A wearer's consent is not a substitute for applicable participant consent requirements. Test this interaction rather than relying solely on a settings checkbox.

Later, on Mac, the same task and source appear. The agent can draft the proposal or an email using approved sources. Sending requires review of recipient and content under the outbound-message policy. If the desktop executor is asleep, the phone says which step is unavailable and offers a draft or reminder. Completion comes from actual task/connector evidence, not from saying “done.”

Pilot success is fewer missed commitments and less cleanup than the user's current method. Measure transcription errors, accepted/corrected tasks, completed outcomes and unwanted interruptions. A beautiful transcript without useful follow-through is a different product outcome.

### Understand the day without making up a diagnosis

The user asks: “How did my day go?” The agent gathers the explicitly enabled sources: conversations, tasks, self-reports and valid sensor observations. It separates observations from interpretation: “Your heart rate reading was elevated during that period; you also said the meeting felt tiring.” It does not conclude that a meeting caused a medical condition.

Every important claim links to time/source and can be corrected. Missing measurements are disclosed. A user-chosen next step might be scheduling a break or noting a question for a professional; a generated recommendation is not written into HealthKit as a measured sample. Track whether adding biosensor context changes a useful decision relative to context-only review.

### Prepare a purchase from the glasses

The user asks: “Find a replacement charger compatible with my laptop, under my budget.” The agent resolves product compatibility, presents sources and prepares a quote through a supported merchant adapter. The wearer hears a short summary with merchant, item and total, then receives the complete frozen terms on the phone. The final review includes tax, shipping, address, recurrence if applicable, return terms and expiry.

The user confirms the exact action and completes required wallet/issuer authentication on the trusted surface. Spoken “yes” alone is not a biometric wallet authorization. The agent reports accepted order status only after merchant/payment evidence exists. A timeout produces “The outcome is not confirmed yet; I am checking it,” with no blind duplicate purchase. Receipts, delivery and refund requests appear in the action inbox.

Initial release can stop at search/cart/checkout handoff while the transaction system is tested. “Pay anywhere by tapping the glasses” is a separate hardware/payment-network program; current Theora health-temple glasses and Theora Eye camera glasses capabilities do not establish NFC payment hardware, secure credential storage or payment certification.

## Consumer information architecture

| Surface | Primary content and purpose |
|---|---|
| Conversation | Text/voice threads, sources, attachments, task progress and interrupted requests |
| Today | Commitments, chosen routines and a concise optional day review |
| Memory | Remembered facts and preferences with sources, corrections, deletion and export |
| Action inbox | Pending approvals, quoted terms, execution status, unknown outcomes and receipts |
| Devices | Brain selection, glasses/wristband capabilities, connection/freshness, battery and diagnostics |
| Settings | Account, consent scopes, provider/deployment choice, avatar/voice, integrations and accessibility |

On iPhone, prioritize existing native SwiftUI surfaces and consolidate the two assistant targets behind one agent experience. On desktop, retain Tauri while bringing the consumer client into the shell and wiring OS features. The advanced dashboard remains an optional expert view. “Native enough” means correct keyboard/menu conventions, file dialogs, audio permission behavior, accessible focus, notifications, secure storage and predictable launch/sleep behavior—not only rounded window chrome.

For Linux, explicitly test supported X11/Wayland distributions and audio/shortcut/tray behavior. For Mac, test fresh install, microphone denial, shortcut activation, multiwindow focus, menu behavior, login startup and app termination without orphaned brain processes. Phone and desktop share semantic behavior, not identical screen layouts.

## Avatars people can choose

Ship a small catalog first: a FERAL-inspired character, a calm abstract orb and a neutral static option. Names and final styles remain product/art decisions. Users may choose none. Add a clear voice selector and a restrained interaction-style setting; do not combine a cosmetic preference with clinical claims or permission changes.

Proposed owner preference contract: `avatar_id`, `asset_version`, `renderer_version`, `voice_id`, `interaction_style_version`, `reduced_motion`, `static_mode`. Asset references must be from a trusted catalog, not arbitrary executable downloads. Preferences sync through same-owner continuity and apply independently of model/provider choice. Retired assets need a predictable fallback.

Drive the avatar from actual state events: idle, listening, transcribing, thinking, executing, awaiting approval, speaking, offline and failed. Associate events with the active turn/action so late events cannot revive an old animation. Approval-needed is distinct from generic thinking. Microphone-off never appears as listening. Errors have accessible text and recovery controls. No animation may obscure an approval, receipt or stop button.

Use audio amplitude for optional speaking animation after testing latency and interruption; never imply the character is hearing or processing when it is not. Do not map physiological readings to a claimed emotion or avatar mood without an explicit justified feature and consent. Avoid language claiming the avatar is conscious or replaces human support.

Benchmark a portable animation runtime against native/simple SVG/static approaches before committing. [Rive runtimes](https://rive.app/docs/runtimes/getting-started) are one candidate, with an [MIT-licensed runtime](https://github.com/rive-app/rive-runtime/blob/main/LICENSE); editor costs and rights to artwork are separate questions. Measure startup time, frame cost, memory, battery and Swift/web accessibility. Supply static/reduced-motion, high-contrast and low-power alternatives with full feature parity.

Custom uploads or generated avatars can follow after catalog validation. They need asset-size limits, rights/distribution terms, moderation appropriate to published/shared assets, removal/export behavior and renderer sandboxing. They are not necessary to test the agent's primary benefit.

## Onboarding and trust controls

First demonstrate one useful interaction without a long questionnaire. Explain where the brain runs, then pair a device, choose permitted capture/model flows and offer avatar selection as skippable. Show each permission in context: microphone, Bluetooth, health source, cloud-model egress, connector OAuth and spending. A successful pairing must not grant all of them.

Provide pause capture, pause proactive suggestions and disconnect/unlink controls. Health data is not required for general tasks. Local-only operation has visible limitations. Notification-denied and background-suspended modes have useful fallbacks. Data retention and recording indicators are understandable before capture, not buried in developer diagnostics.

Proactivity starts conservatively: user-chosen routines, high-confidence commitments and quiet-hour/rate limits. Ask users whether suggestions helped; do not infer permission for more interventions from repeated usage. Prefer one actionable prompt to a feed of speculative insights. Evaluate interruption burden and emotional dependence alongside retention.

## Painful problems worth testing

The following are differentiation hypotheses, not verified market gaps:

1. Remember the exact agreement and finish the follow-up across phone and computer, with evidence and correction.
2. Explain what the agent knows and where it learned it, then let the person change or remove it everywhere.
3. Combine daily context and trustworthy measurements without confusing sensor estimates with facts about emotion or disease.
4. Continue useful capture when disconnected and recover without duplicated tasks, turns or purchases.
5. Change models or hosting while retaining portable memory, preferences and supported workflows.
6. Make a purchase review comprehensible from glasses and verify what actually happened afterward.

Each requires comparison with the user's actual workaround and competing products. Interview discoveries should narrow this list rather than turn every hypothesis into the first release.
