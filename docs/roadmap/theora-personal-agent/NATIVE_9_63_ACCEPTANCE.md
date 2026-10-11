# Native 9.63 profile adoption acceptance

October 10, 2026. Candidate 2026.9.63/build2026101012 contains exact source
`2cab48e5f62c6c7bb0f1b8e42b8c5dc29f54c34a`. Its fresh optimized compile uses
55 Swift inputs. All 509 packaged Python and 70 WebUI files match source;
bundle dependency/import checks and strict deep ad-hoc signature verification
pass. Native SHA-256 is
`e7e74f9557522dff4038800a87e2c0dc50382da3b047e29f0b652302ce8bdf4e`.
Manifest SHA-256 is
`44b9f80182d9a295769387c4247397377340b93d622c7d7992a01dc4a6714b6d`.
Build free space is 16,800,538,624 bytes before and 16,737,361,920 after;
the canonical candidate and sole 9.52 rollback are retained.

## Actual native UI and packaged runtime

Ordinary app activation first showed the unchanged preview default. Settings
prepared a review of the existing personal profile. Saving the next-launch
choice retained the current preview root, fenced new admission and displayed the
restart requirement. Normal Quit followed by ordinary app activation used the
saved personal choice without a FERAL_HOME launch override. The actual UI and
visual screenshot showed that profile and a connected local agent.

Read-only identity verification established exact native PID70604, bundled child
PID70611, the saved port9090 listener, matching runtime metadata and a fresh
X-Feral-Desktop-Instance health header. The existing vault review/unlock in the
actual native UI succeeded and reported credentials available and memory working
data available. No reset, new key, profile copy or alternate provider was used.
Full agent bootstrap, sync and funded inference are separate from vault readiness.

A guard-only second packaged interpreter, using the same existing profile and
a different port, received RuntimeProfileBusy. It did not import api.state,
api.server or api.keys and did not start a second server. The existing lock inode
was preserved. This establishes packaged cooperating admission refusal, not
universal exclusion of older or offline writers.

Normal native Quit subsequently removed both verified processes and the9090
listener. A guard-only packaged admission could then acquire and release that
same lock, preserving its inode and without importing backend/vault state. The
immediate post-Quit socket-bind preflight refused with errno48; that refusal did
not launch another app. Its cause has not been established. A later unchanged
preflight passed. Further ordinary activation again used the saved choice and
verified nativePID72537/bundled childPID72543/9090 ownership and instance health.

Devices showed the phone unavailable and zero registered nodes. Peripheral
readiness labels do not establish a connected phone, new sensor readings or
physical commands. No phone interaction or live microphone test is claimed.

## Remaining acceptance

The previous actual regular-Chrome model workflow reached selected scene
inference, then failed on provider credit, with no itinerary/draft saved and no
replay. This candidate does not claim a newly completed browser task. Funded
model-driven date entry, verbatim code selection and independent saved-result
verification remain required. Physical HFP duplex voice, interruption, ambient
rejection, task concurrency and completion reporting remain required.

The development iOS historical BP/HRV sample-time repair is separately signed
and installed at local4a6b9d9. Fresh vitals forwarding and durable history/baseline
acceptance are separate work. The installed legacy application is unchanged;
ad-hoc signing, normal profile adoption and source publication are not public
distribution signing, notarization, clean-machine installation or PyPI release.

Parent fixtures for profile selection pass87 layout/61 selection assertions and
23 linked assertions within30 model wire groups, plus39 desktop/five error checks.
Startup writer fixtures pass38 tests; expanded disposable CLI/WebSocket checks
pass26 with one skip. Full-core CI Ruff passes; Mac typing retains779 diagnostics
with zero additions/removals. Remote PR310 at79cb09e3 has18 successful checks and
four conditional skips; new2cab48e5 candidate source is not yet represented by
that completed CI run.
