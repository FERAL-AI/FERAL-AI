# Native action admission during profile shutdown

October 3, 2026. Extends the existing native HTTP clients and runtime lifecycle.

A source review found that a retained feature model could finish an earlier
passive read and dispatch a write after archive shutdown began. The actual Agents
model reproduced this with mocked HTTP: a held read followed by one spawn POST.
Disabling navigation or clearing only the currently visible endpoint did not
invalidate a detached task retaining its old model.

The shared gate binds each HTTP session to a captured local-origin epoch. Pausing
the exact owned runtime closes admission synchronously. A newly verified runtime
can activate a fresh epoch; an old session cannot inherit it, even at the same
address. Weak session storage and origin/session limits bound retained state.
Capacity exhaustion refuses admission. Unmanaged nonlocal transport behavior is
unchanged. The guard checks before dispatch and after response, without reading
request bodies or credentials.

Twenty-eight HTTP transport calls in27 feature files use this guard. NativeModel
also blocks its own requests, voice, recovery, chat tools and profile mutations
while stopping. Its private exact final-conversation-save path remains available
and verifies the selected ID and message count. Voice and recovery disconnect
before the first archive-save await. Shutdown closes admission before waiting for
archive-task cancellation. Fresh feature sessions follow runtime replacement;
the live archive pane is preserved. Settings sheets close during the pause.

## Verified boundary

- **34 gate assertions passed**, including the actual retained Agents model:
  paused and same-address replacement cases both dispatched zero spawn POSTs;
  stale sessions refused, and a fresh reviewed registration/readback succeeded.
  The prior passthrough transport dispatched one POST and failed the regression.
- **42 existing Agent assertions passed** with the guarded transport.
- A separate linked-model delayed-final-save check passed: coding task, workspace
  grant, coding provider and model-setting requests dispatched zero writes while
  the exact final save remained verifiable. That run passed27 mocked model groups.
- Gate source SHA-256:
  `8d58e12861fa6ceb8139870e178db050109a9abf3862b7b33a653356098aec52`;
  gate test SHA-256:
  `33a5cc35c496709066e7bf2d76de1f427c344483a1ac9d95499f4fcf41987214`.

Already-dispatched effects remain uncertain. A late receipt is refused and the
old session cannot automatically replay it; this does not reverse an effect or
create a cross-process writer lease. Exact owned runtime shutdown remains
required before the offline archive CLI. Actual GUI/file-picker, bundled archive
host, account and OS audio acceptance are separate gates.

Local commands, from `desktop-native`:

```sh
bash build.sh --typecheck
bash test_features.sh LocalActionGate Agent Workflow Integration Configuration ProfileArchive Voice
```

The final integrated command and complete production typecheck both passed.
The runner also passes42 Agent/83 Workflow/80 Integration, Configuration checks,
280 archive/55 voice assertions and27 linked model groups/39 desktop/5 error
checks. Existing Swift5 fixture NSLock warnings remain. Source checks and mocked
HTTP are distinct from packaged-runtime and GUI execution.
