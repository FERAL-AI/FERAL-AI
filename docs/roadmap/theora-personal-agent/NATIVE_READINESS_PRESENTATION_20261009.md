# Native provider readiness presentation

October 9, 2026. Chat receipt negotiation confirms tracked context and receipt
support. It does not probe the selected model or establish successful inference.
The native connected status now identifies the receipt channel and explicitly
separates model availability. Existing chat admission and voice/policy gates are
unchanged.

Onboarding and AI Providers keep a separately labeled last explicit probe result.
An unchanged passive catalogue refresh no longer erases a just-validated probe
whose catalogue reachable field is null. The result is view-local history, not
current model inference, global backend health, CLI state or a cross-view cache.
Absent history says no probe result is available in this view.

History is published only after validated response identity, saved configuration
readback and current review generation. Changed draft, provider, endpoint,
credentials/configuration, runtime descriptor, uncertain operation or retired
connection clears it. Probe-produced reachability/discovery observations do not
invalidate their own result. Cached refresh performs no live probe. Existing
request-return cancellation/generation fences and immediate sameDraft generation
validation prevent a retired review publishing into a replacement connection.

Executed frozen native fixtures pass 75 Providers assertions across 21 groups,
100 Onboarding assertions and 29 linked model groups. Production 54-source
typecheck passes. Tests cover negative/positive probe receipts with null passive
catalogue, unchanged passive refresh, drift, malformed/private errors, retired
connections and receipt-only ready wording with unchanged admission. No live
model/provider/account/microphone operation occurs. The initial macro-service
sandbox failure is retained; identical source passes outside that sandbox.

Private receipt: feral-oct9-native-readiness-fix-evidence.json. Six source/test
files are integrated in published exact-source 9.49. Actual isolated setup and
Providers probes retain history through passive refresh, draft editing clears it,
and Chat receipt-only status persists across normal Quit/relaunch.
[Packaged acceptance and limits](NATIVE_9_49_ACCEPTANCE.md). These changes do not install a local model or solve physical voice,
phone networking, background device approval provenance or distribution gates.
