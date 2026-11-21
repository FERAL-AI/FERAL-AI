# Native 9.38 existing-Chrome acceptance

Recorded October 4, 2026. Actual app observations and harness lifecycle results
are separate. This is not personal-profile, account or distribution acceptance.

## Artifact

| Item | Value |
|---|---|
| Version / build | `2026.9.38` / `2026100402` |
| Runtime source | `d5c063605e4991dcc0efd1396854eecca7968957` |
| Native SHA-256 | `887cabcdbb31b3e983f6439eb3255fc5a6226b7e05ad98b6d405a0d65d8ab0ac` |
| Manifest SHA-256 | `d468d8b6f284a04aaeb81603a7ee18b937f8535416dd1076c63261e9df01dd30` |
| Input equality | 52 native inputs; 492 packaged Python files |

Optimized cached/offline assembly, bundled-runtime probes, source equality and
strict ad-hoc signature checks passed. Build receipt:
`/private/tmp/feral-native-9-38-build-20261004/result.json`.

## Actual GUI observations

The fresh app profile used the shipped packaged backend, deferred optional vault
setup and the existing cached local model catalogue. No inference or download was
needed. A separately owned Chrome used a synthetic loopback page. Only its
two-line endpoint file was placed under the isolated app user's default Chrome
path. No CDP environment override, personal profile copy or account access was used.

| Behavior | Observed result |
|---|---|
| Avatar and navigation | Companion selection and all 22 destinations retained |
| Local setup activation | Reviewed activation and settings readback succeeded |
| Saved-provider probe | Backend returned HTTP 200; native parser incorrectly rejected normal `error: ""` |
| Connection cancellation | Cancelling the connection review left Browser disconnected |
| Connection and selection | Explicit connection review returned one synthetic tab; separate selection review attached it |
| Viewing | Separate consent displayed actual Chrome JPEG frames; password and iframe regions were hidden |
| Central approval | One explicit owner-session click appeared in Oversight; native approval executed the reviewed `#confirm` request |
| Outcome and marker | Viewed page showed `Synthetic click confirmed` and an orange input-location marker; both field values remained original |
| Navigation and Stop | Leaving Browser retired viewing; returning required new viewing consent; Stop removed the frame |
| Disconnect | Native connection controls disappeared and Start viewing became disabled |

The click was queued through the registered tool HTTP/executor path and approved
through the actual native UI. It was a synthetic operator-requested action, not
a model-selected task. A successful click receipt alone was insufficient; the
changed page label and marker were then observed through Computer Use.

## Retained failures and next acceptance

The GUI harness's 840-second deadline elapsed before normal Quit was captured.
Its exact owned host was retired and both Chrome listeners closed. The receipt
reports `success: false`, `normal_quit_observed: false`; this run does not pass
the lifecycle gate and is not evidence of an application crash. Receipt:
`/private/tmp/feral-native-populated-9-38-fresh-20261004-attempt-existing-browser1/existing-chrome-gui-wave-result.json`.

The real provider-status shape reproduced the setup parser defect in a regression
fixture before correction. The fix must be packaged and checked in the next app.
Oversight also incorrectly described a standing session grant for a resource-bound
browser approval. Those approvals authorize one reviewed request; accurate typed
scope presentation is required before the next packaged acceptance.

Source verification for d5c comprises 1,468 backend checks across 53 suites with
three opt-in skips; native Browser 115 checks and Oversight 12 groups plus linked
model/desktop/error checks. Ruff passes; full mypy remains at its 809-error
baseline. Actual personal Chrome permission, signed installation, account tasks,
real audio, glasses, Messages and commerce remain unperformed.
