# Mac chat completion and configured output allowance

Updated October 3, 2026. This is source/fixture evidence for the correction after
immutable candidate 9.35; it does not certify a rebuilt app or external account.

## Confirmed foreground completion defect

The actual 9.35 transcript contained the main answer before its WebSocket closed.
The exact durable receipt was cancelled/unknown with empty final text. The runner
awaited optional skill discovery after the main command, before the manager could
commit the terminal. That follow-on request was still pending when detachment
cancelled the foreground task. A recovered answer alone did not prove completion.

Tracked turns now schedule retained optional follow-up in a separate context,
without inheriting the foreground audit or writer. Follow-up waits for the exact
foreground task and verifies its durable terminal before discovery. Captured
socket, session, store, agent generation and committed checkpoint guard proposal
generation/delivery. Disconnect or a newer context discards undelivered proposals;
generated skills are never automatically approved or installed. Failed terminal
storage prevents discovery. Existing untracked behavior remains unchanged.

## Confirmed output-setting defect

Saved `llm.max_tokens=512` previously produced normal request bodies containing
1024. The provider now resolves the saved allowance when ordinary chat omits an
explicit limit. Both real orchestrator paths, their existing empty retry and
cross-provider failover preserve the selected value. Explicit background/worker
limits keep precedence. Invalid values produce `llm_configuration_error` before
budget reservation or model traffic; settings endpoints reject invalid writes
before changing credentials, identity or profile data. Null patch deletion
restores the default 1024.

Ollama request-capacity checks use the selected allowance. Cloud history-fit
heuristics are unchanged. Provider-specific thinking headroom and the existing
cost-reservation behavior remain authoritative, so this is not a universal hard
output or spending cap. No native output-limit editor was added.

## Parent integration and limits

The parent ran 25 suites in a fresh isolated home with offline caches/null keyring:
**623 passed**, 219 warnings, 24.89 seconds. All 1,291 Python inputs remained unchanged
before/after tests, lint and typing. Their shared fingerprint is
`9939c8aa570f661064364aa4a737ea3e6bd853cd3fbf1ee39828cdd61850b875`.
Core Ruff passes. Full mypy reports 809 existing diagnostics in 233 files, with
zero added/removed normalized diagnostics versus the retained 809 baseline.
Typing is not clean.

Focused evidence includes actual SQLite terminal notification before optional
detection; pending/cancelled/throwing detection; replaced socket/store/generation;
committed and advanced attached checkpoints; replay; undelivered manifest cleanup;
terminal-store failures; real HTTP request bodies; rejected settings mutations;
fallback routing and thinking-provider transformations. Provider traffic is mocked
in these fixture checks; the original failed journey used a real local model.

Native passive-health suspension is a separate active correction. Rebuild and
repeat actual tool-enabled chat, exact terminal recovery and restart before
claiming reliable app acceptance. Physical audio, cloud accounts and distribution
remain separate gates. [Current work](WORK_STATE.md) and
[9.35 evidence](NATIVE_9_35_ACCEPTANCE.md).
