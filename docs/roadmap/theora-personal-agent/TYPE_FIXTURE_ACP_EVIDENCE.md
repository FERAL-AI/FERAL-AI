# TYPE01I/J: provider fixtures and ACP permission-context typing

Date: 2026-10-02. Checkout HEAD observed as
`dd69c7bf5175517966a363591fa8137782358535`. This card owns only
`tests/test_failover_endpoint_routing.py`, `bridges/acp.py`,
`tests/test_acp_permission_context.py` under `feral-core` and this new evidence
file. Parent owns publication and the shared checkpoint. TYPE01H's three files,
the immutable 9.30 app and staged resources were not edited.

## TYPE01I: real supported fixture wiring, ten diagnostics

The annotated test helper passed explicit None to required-annotated
collaborators, assigned its provider to a None-inferred attribute, directly
replaced three methods and inspected AsyncMock recording through a production
Callable annotation. Source inspection confirmed Orchestrator's actual
constructor deliberately defaults these collaborators to None and supplies a
PerceptionEngine when none is given. Omitting them uses that supported ordinary
in-memory-history configuration; no production type is widened or annotated
for a test double.

The helper now retains an explicit provider mock and AsyncMock recorder, wires
the provider through existing `set_llm`, and uses scoped monkeypatch replacement
for prompt-routing, temporal timeline and system-prompt methods. Its callers
inspect the retained collaborators rather than pretending the production
Callable exposes mock-only recording attributes.

Existing assertions remain: Responses-only failover candidates use Responses
on the correct endpoint/client, ordinary chat candidates keep Chat Completions,
empty Responses streams never fall through into another request, provider
errors are explicit error frames, errors produce no final-answer/assistant
history, and multi-agent errors cannot trigger a hidden single-agent retry.
The successful-text control still requires ordinary assistant text/history.
No real provider or model connection was added.

Measured focused file diagnostics: **10 before, zero after** under the same
repo-configured mypy command. This does not claim every untyped test body is
checked: mypy continues to report its existing annotation-unchecked notes.
Preexisting ignores elsewhere in this suite were not expanded or changed.

## TYPE01J: exact ACP session before scope lookup, two diagnostics

`AcpAgentProcess._permission_context` fills a missing read target only from the
same live session/tool call's consistent captured rawInput and locations. It
never derives consent from a title, a foreign/completed call or changed scope.

The method now checks that sessionId is a nonempty string before dictionary
lookup. Missing, empty and unhashable malformed IDs return the untouched
request; nothing is coerced, trimmed or mapped onto another session. The
heterogeneous captured-field mapping is explicitly `dict[str, object]`, since
raw event values remain untrusted. Existing rawInput shape checks, exact
session/call/kind comparisons, terminal-status refusal and disagreement checks
are preserved; the annotation does not give captured events additional
authority.

Twelve added controlled cases cover missing/wrong/malformed session IDs,
changing locations within the same live call and locations that conflict with
the incoming request. Existing same-call enrichment and no-input-mutation,
foreign identity, completed call, kind mismatch, missing scope and changing
rawInput cases remain intact.

Measured focused ACP diagnostics: **four before, two after**. The removed two
are permission-context session lookup and untyped snapshot. Older subprocess
stdout/stdin Optional diagnostics at lines 434/435 are deliberately unchanged;
this card does not prove or repair subprocess startup pipe lifecycle.

## Commands and measured checks

From `feral-core`:

```bash
env FERAL_HOME=/private/tmp/feral-type-fixture-acp-home FERAL_DATA_HOME=/private/tmp/feral-type-fixture-acp-home FERAL_AUDIT_LOG_PATH=/private/tmp/feral-type-fixture-acp-home/audit.log ../.venv/bin/python -m pytest tests/test_failover_endpoint_routing.py tests/test_acp_permission_context.py tests/test_bridges_acp_client.py -q --tb=short --no-cov -p no:randomly --timeout=60
../.venv/bin/python -m mypy -m tests.test_failover_endpoint_routing -m bridges.acp --follow-imports=silent --no-incremental
../.venv/bin/python -m mypy -m tests.test_failover_endpoint_routing -m bridges.acp -m tests.test_acp_permission_context --follow-imports=silent --no-incremental
../.venv/bin/python -m ruff check tests/test_failover_endpoint_routing.py bridges/acp.py tests/test_acp_permission_context.py --select=E,F,W --ignore=E501,E402,F401,W291,W293
```

- Tests: **76 passed, 6 warnings in 8.24s**. The existing controlled-child
  environment-restoration warning is reported rather than suppressed.
- Two-module mypy: **14 before, two after**. The third permission-test module
  adds no diagnostics in the final three-module run; both remaining errors are
  the older ACP pipe Optional annotations. The checker exits one accordingly.
- Exact Ruff gate and scoped `git diff --check`: passed.

Private logs: `/private/tmp/feral-type-fixture-acp-{before,after,final}.log` and
`/private/tmp/feral-type-fixture-acp-tests.log`.

## Limits

No new Any annotation, cast, ignore or type-baseline change was introduced.
Followed-import errors are suppressed by this focused check; no measured global
or Ubuntu diagnostic count is claimed. No account, provider, real OpenCode
binary, download, model, personal profile, native build, GUI or staging operation
was used. Passing these controlled protocol/fixture suites is separate from
live coding-agent and packaged-native acceptance.
