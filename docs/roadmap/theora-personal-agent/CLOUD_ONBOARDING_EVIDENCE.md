# Reviewed cloud credential setup

October 2, 2026. This change wires fresh secure credential setup into the existing
native onboarding and vault controls. It does **not** establish working cloud
onboarding in the current ad-hoc app: signed OS acceptance and actual provider
inference remain open.

## User-visible behavior

When a selected cloud provider needs a key and credential storage is unavailable,
onboarding shows a separate encrypted-storage setup control. It reads passive
status, prepares a server-issued review, requires explicit confirmation and
verifies the operation/token/outcome before independent setup-status and vault
readback. The provider key is entered and saved through the existing provider
configuration route only after storage is authenticated. Storage initialization
never sends a provider key or model request.

Existing storage has a separate unlock panel. Existing keys, plaintext/ciphertext
artifacts, partial initialization, an operation already in progress or unsupported
secure storage cannot become a fresh initialization. There is no reset, overwrite,
key deletion, automatic migration, plaintext fallback or automatic action retry.
Cancelling an unused review revokes it. Cancelling an HTTP wait after confirmation
cannot undo an OS action; the client retains an unconfirmed state until passive
status reconciliation. A late result cannot populate a replacement local service.

## Runtime wiring and release gate

The deferred BrainState binds the existing BoundVaultInitializer and
VaultInitializationAPI to the **same coordinator and credentials.json target**
as its existing read-only unlock factory. The existing initialization router is
mounted in the server and retains its normal authorization/middleware boundaries.
No second credential store or coordinator is created. Local operation remains
available without creating a vault.

The release gate is supplied by a version-bound receipt in the signed app's
Info.plist, derived from the running bundled interpreter. An API argument or
environment variable cannot enable it. The receipt must contain:

- accepted: true;
- scope: macos-keychain-add-if-absent and contract_version: "1";
- bundle_id and build exactly matching CFBundleIdentifier and CFBundleVersion;
- a ten-character developer team ID and bounded acceptance_id identifying the
  recorded OS acceptance.

The verifier checks the receipt shape, bundled interpreter location, Developer-ID
Application requirement, deep/strict signature integrity and matching team. Each
codesign subprocess has a five-second limit. Missing/malformed receipts, development
interpreters, ad-hoc signatures, changed builds, mismatched teams and unsupported
platforms remain closed. Passive reads do not query credential contents or OS keys.
A signature proves publisher/integrity; **it does not prove OS behavior was tested**.
The release process must perform and record the actual key-storage contract before
issuing this receipt and signing the app. This work does not issue a production
receipt, acquire a signing identity or alter the frozen 9.30 app.

## Checks performed

All native checks use an ephemeral URLProtocol fixture. No OS Keychain item,
provider account or personal deployment is accessed:

- New native VaultSetup suite: **31 assertions passed**, covering passive reads,
  prepare/confirm/readback, exact one-use token, cancellation, expiry, artifacts,
  changed origin, denial/mismatched receipt, missing independent readback, partial
  persistence, unavailable release gate and late/cancelled HTTP responses.
- Existing native onboarding suite: **34 assertions passed**.
- Existing native vault/continuation suite: **43 assertions passed**.
- Focused backend vault/release suites: **92 tests passed, seven warnings**. These
  include actual production server router registration, actual deferred BrainState
  binding, and existing real encrypted-file/AEAD tests with a fake atomic
  add-if-absent OS adapter. They are not real signed Keychain acceptance.
- Ruff and whitespace checks passed for the new Python sources/tests and router.
- Focused mypy with check-untyped-defs reported no diagnostics in the new release
  verifier or initialization router; imported dependencies still failed. This is
  not a clean full-project type gate.

The first focused backend invocation passed its tests but failed the unchanged
50% whole-project coverage floor because only vault tests ran. The focused rerun
used an explicit diagnostic coverage floor of zero and a disposable coverage file.
The required whole-project/CI coverage threshold is unchanged.

## Remaining acceptance

Production native typecheck/linked-model and broader integrated backend checks
belong to the parent integration freeze. The current app and latest source are
different artifacts; no candidate assembly or GUI test was performed by this card.

A supported fresh-cloud installation still requires a signed installed app with
actual macOS add-if-absent success, duplicate-key refusal, denied/missing Keychain,
partial disk failure and restart/readback acceptance. Then an explicitly authorized
real provider key save, activation, inference and relaunch must pass. Actual cloud
login/subscription support is provider-specific and is not inferred from API-key
storage. Current ad-hoc builds truthfully show release_acceptance_required and
allow local-provider use.
