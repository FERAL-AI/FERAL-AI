# Native 9.39 acceptance ledger

Updated October 4, 2026. The candidate is scheduled as
`2026.9.39/build2026100403`. Assembly and actual GUI acceptance are pending.
The immutable predecessor and its failed lifecycle gate are recorded in
[9.38 acceptance](NATIVE_9_38_ACCEPTANCE.md).

## Corrections and source verification

- Saved-provider probes parse the actual ProviderStatus receipt, including
  `error: ""` on success, while rejecting contradictory/malformed identity and
  Boolean fields. Private unreachable diagnostics are withheld. Other setup
  endpoints retain their existing error refusal.
- Approval inbox responses preserve an optional exact browser-resource scope.
  Native rows, reviews and receipts distinguish one reviewed browser request
  from ordinary session permission. Invalid or changed scope disables decisions.
- The Chrome skill consumes a pure shared security session validator instead of
  importing memory. The durable checkpoint wrapper preserves its public exception,
  message and exact validation. Python SDK tests provide explicit caller identity
  and prove missing/invalid identities produce zero execution; SDK behavior is
  unchanged and does not invent an HTTP tool identity.

| Check | Result |
|---|---|
| Frozen backend integration, 54 suites | 1,567 passed, three opt-in skips, 239 warnings, 52.72 seconds |
| Backend input equality | All 1,307 Python inputs unchanged; digest `76757f4bbf94bef92a1d93b44a4866f22b65de37b86dbb595f811e32985c5a8b` |
| Integrated native features | Onboarding 73 assertions; Oversight 16 groups; Browser 115 checks; linked model 28 groups; desktop 39; errors five |
| SDK contracts | 121 passed; seven warnings; 3.54 seconds |
| Validator/adapter/checkpoint focused suites | 245 passed; one opt-in skip |
| Architecture boundary and Ruff | Pass |
| Full mypy | 809 errors in 233 files; zero normalized additions/removals relative to baseline |

The full type check is not clean. Native fixture compilation retains pre-existing
Swift 6 async NSLock warnings in linked tests; the runner uses Swift 5 mode.
Backend/native/SDK counts overlap and are not a total product-readiness measure.

Private receipts/logs:

- `/private/tmp/feral-chrome-native-939-integration-20261004-tests-6xtna_1f/receipt.json`
- `/private/tmp/feral-native-939-integrated-feature-tests.log`
- `/private/tmp/feral-native-939-typing-delta.json`
- `/private/tmp/feral-sdk-session-ci-fixed-_qvtzqz8/receipt.json`
- `/private/tmp/feral-session-validator-ci-fixed-kma7bu8o/receipt.json`
- `/private/tmp/feral-native-oversight-resource-tests.log`

The provider-parser regression was reproduced before correction. A separately
registered Python approval response was also parsed by the native executable,
in addition to native HTTP fixtures. These are source/contract checks; they do
not prove the next app's actual setup, action or lifecycle outcome.

## Next artifact gates

Build only committed inputs with cached dependencies, strict ad-hoc signature,
bundled-runtime probes and packaged-source equality. Budget at most 2 GiB of new
artifacts with at least 10 GiB free; keep canonical plus one preceding app copy.

Then test a fresh isolated profile through actual companion/setup UI, successful
saved-provider probe, explicit existing-Chrome/tab reviews, scoped native approval,
observed synthetic page outcome, Stop/disconnect and normal Quit. Require exit 0
and closed backend/browser listeners. Preserve all failed harness evidence.

Personal Chrome permission/account use, real audio, glasses, Messages, payment,
clean-machine install, migration, updates and signed distribution remain open.
This build does not certify Linux or iOS. See [release gates](RELEASE_READINESS.md).
