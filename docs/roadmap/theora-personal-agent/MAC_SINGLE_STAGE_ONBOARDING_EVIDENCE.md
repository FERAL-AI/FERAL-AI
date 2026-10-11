# Single provider stage after avatar selection

Updated October 3, 2026. This repair removes the duplicate provider flow observed
in the actual isolated 9.34 Mac app. Source and mocked-network results remain
separate from acceptance of the rebuilt app.

## Behavior

Avatar and name selection now advances directly to the existing reviewed
provider feature. The old local-only onboarding form and alternate-provider sheet
are removed. That path previously wrote Ollama settings even after selecting a
cloud provider, then opened provider setup again.

The single provider stage preserves local/cloud configuration review and exact
readback. Verified completion enters the main app without another activation.
Set up later only dismisses the stage; it performs no provider write or probe.
AI Providers and explicit setup reopening remain available. Existing profile
data, avatars and provider settings are preserved.

A local setup-stage identity, runtime generation and service origin fence late
completion callbacks. A reply from a dismissed stage cannot close a reopened
stage. Missing preferences, paused effects or an unready runtime cannot advance
the applicable transition. These local navigation guards do not replace backend
authorization or certify successful inference.

First-run copy is shortened; detailed destination and effect reviews remain
available at the relevant action. Voice and glasses setup remain optional later
steps.

## Verification boundary

The linked fixture uses the actual NativeOnboardingSetupModel and NativeModel
with mocked HTTP. It exercises local/cloud activation and exact readback,
completion, zero-effect skip, restart, failed readback, changed saved settings,
unavailable preferences, paused effects, and stale origin/runtime/stage callbacks.
The parent combined run passed all 33 onboarding assertions and the existing
34 onboarding-feature assertions. Native production typecheck passed without
warnings. All 122 native source/test/harness inputs remained unchanged during
the final feature/linked run and production typecheck. The linked fixture build
reported 18 async-lock warnings; it passed under the repository's Swift 5 mode.
These results do not establish Swift 6 migration readiness.

The earlier actual 9.34 journey establishes the observed duplicate, not acceptance
of this repair. The exact-source 9.35 app must exercise avatar selection, one
provider stage, completion and entry to chat. Credentials, microphone permission
and real cloud-account operation were not exercised by these fixtures.
