# Web setup catalogue and probe parity

The setup wizard now uses passive cached model inventory, respects runtime
support flags and preserves a validated explicit reachability probe through
passive refresh. Unknown reachability is shown as unknown. It no longer sends
a live model discovery request merely because the selected provider changes.

Providers with runtime_supported and setup_selectable both true are selectable.
Legacy entries with both flags absent remain unconfirmed compatibility entries.
False, partial or malformed flags are read-only. A saved unsupported selection
stays visible instead of being silently replaced; this change adds no gateway
configuration or model installer.

Explicit probe history is tied to a captured draft revision, saved configuration
and catalogue descriptor. Provider/model/key edits retire it, even after restoring
the old value. Changed saved settings, capability flags, failures and late
responses cannot publish earlier results. The API client can raise ApiError for
a legitimate negative probe returned at HTTP 200; only an exact successful-HTTP,
provider-matched, boolean-false receipt is accepted from that error wrapper.
Private diagnostics are not displayed as public readiness text.

Save advances only after an exact persistence receipt and readback matching the
provider/model, expected endpoint/fallback semantics and supported capability.
Changes during the write/readback or navigation retire its publication authority.
A failed verification reports that settings may already be saved and requires
refresh before retry. Settings persistence is distinct from runtime activation
or a successful model inference.

Probe measures the provider catalogue connection. It does not verify the active
model endpoint, unsaved credentials, inference, FERAL tool use or voice. The
existing backend explicitly binds saved local endpoints. In preceding 9.50,
generic custom cloud endpoints can differ from catalogue defaults after restart
or key changes. Follow-on source aligns active cloud connections at startup and
all settings/key activation paths; [behavior and validation](ACTIVE_CLOUD_CATALOG_20261009.md).
It does not make a reachability probe proof of model inference.

The existing CLI can provision Ollama and consented local speech assets; this
wizard and the native app do not yet expose a complete reviewed installer.
The Mac bundle supplies its declared runtime dependencies, not Ollama, speech
engines or model weights. Installing, configuring, reaching and successfully
running a selected model remain separate acceptance states.

Focused final verification passes 48 tests across existing setup, pairing-step
and new provider-parity suites. Full WebUI Vitest passes 1,409 tests across 174
files (186.13 seconds); all 367 inventoried source/configuration and backend/
desktop sibling inputs remain unchanged. Cases exercise HTTP negative receipts, invalid
schema/support flags, unknown status, draft/config drift, delayed inventory,
probe/save/readback/navigation, duplicate probe dispatch and saved unsupported
selection. The WebUI rebuild and checked-in model-picker contract also pass.
All 70 packaged WebUI files match source in Mac 9.50. Actual Chrome UI against
that backend renders setup, read-only unsupported choices, unknown reachability,
an explicit negative local catalogue probe and retained history on passive
refresh. Welcome Continue accessibility activation did not advance; direct
AI Provider tab selection did. No inference, model download, private account or
physical-device test is performed by these checks.

This source is included in immutable Mac 9.50, not preceding 9.49. Build identity
and scope are in [9.50 acceptance](NATIVE_9_50_ACCEPTANCE.md) and
[WORK_STATE](WORK_STATE.md).
