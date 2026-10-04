# Existing Chrome connection contract

Updated October 4, 2026. The connection adapter, local HTTP routes and native
review controls are implemented in the current integration tree. Immutable
2026.9.37/source `b9afadb236e6e39d798a311d38955bb9e8051e74` predates this
connection feature; its packaged-browser acceptance is a separate record.

## Connection and consent

FERAL connects directly to an already running Chrome through its bounded
`DevToolsActivePort` discovery file and browser WebSocket. It does not install or
run Chrome DevTools MCP, require an MCP package, launch Chrome, copy a profile or
supply Chrome command-line switches. The Mac production path addresses Chrome's
existing default user-data root; a different profile can be supplied only by the
controlled adapter fixture. This is not a verified Linux or Windows existing-profile
connection.

Chrome 144+ documents permission-based debugging through
`chrome://inspect/#remote-debugging`, including Chrome's own connection permission
review. That browser permission is independent of FERAL's review. The adapter
checks the reported browser version and refuses unavailable, malformed or unsupported
endpoints. Actual permission behavior in a personal default profile remains
untested. [Official Chrome connection documentation](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md#connecting-to-a-running-chrome-instance).

The operator reviews a connection for the selected chat, grants strict boolean
consent, then reviews the exact tab selection. Discovery returns tab IDs and
bounded titles only after consent. Labels are held in memory, exclude URLs,
strip Unicode control/format characters and are bounded to 180 UTF-8 bytes.
A blank title uses a generic numbered label. A title may itself contain personal
information; this is title minimization, not comprehensive redaction. Titles are
not imported into chat memory by this connection flow.

Chrome's browser permission can expose all windows in the selected profile.
FERAL's selected-tab/session fences constrain its implemented dispatch path;
they do not narrow Chrome's OS-level debugging permission. An existing active
FERAL-owned browser connection must be disconnected before changing modes.
Failure never silently launches a separate browser or adopts another profile.

## Headless interfaces

All routes use the existing application authentication plus a direct local
operator check: `X-FERAL-Browser-View: native-v1`, with remote, forwarded,
`Origin` and `Sec-Fetch` requests refused. The header is a cross-site request
fence, not authentication. The supported baseline trusts local processes; this
is not a multi-user account-isolation boundary.

| POST route | Exact request and result |
|---|---|
| `/api/browser/existing/status` | Validated `session_id`; reads that owner's connection and tab inventory |
| `/api/browser/existing/connect` | `session_id`, literal `consent: true`; connects without selecting or executing a task |
| `/api/browser/existing/select` | `session_id`, canonical UUID `connection_id`, bounded `target_id`; fresh selection rotates the connection identity |
| `/api/browser/existing/disconnect` | Exact `session_id` and `connection_id`; retires owned sockets and reports `browser_closed: false` |

Requests forbid extra fields and coercion. Session IDs use the existing exact
1–1024-character validator. Responses are non-cacheable, return bounded public
error codes and do not return upstream exception text. Status is not inferred
from a prior request: awaited inventory rechecks connection, browser, runtime,
orchestrator, runner and owner identity before returning.

## Task and review ownership

The selected controller uses flattened target-specific CDP sessions on FERAL's
owned browser socket. Selection installs it through the existing BrainState
browser reference and web-actions backing. Failure or cancellation restores the
captured prior implementation when it still owns that reference; it must not
overwrite a replacement runtime or unrelated controller.

Browser and web-actions tool admission captures exactly
`{connection_id, target_id, owner_session_id}` through ToolRunner. Existing
policy, plan-mode, cancellation, approval and executor gates remain authoritative.
A pending approval preserves that captured resource. Reselection, disconnect or
resource replacement invalidates it; inspection does not renew approval. The
backing task reads the trusted captured scope, and low-level CDP checks its exact
resource and calling session before and after awaited dispatch. JSON arguments
and a tab ID alone do not grant action authority.

The connector reuses the current browser controller for its supported selected-page
operations: navigate, click, type/fill, hover, scroll, key/coordinate input,
snapshot, page information, wait and bounded masked screenshot. Other inherited
endpoints refuse, including global tab management, arbitrary public evaluation,
cookies, downloads, recording and unrestricted full-page capture. There is no
fallback to a global HTTP/Playwright browser connection for unsupported work.
Operator viewing uses a separate narrow same-task permit for actual masked
capture; viewing permission never authorizes input. Discovery and setup permits
also remain narrow and cannot be inherited as authority by a child task.

Disconnect and shutdown close FERAL's exact socket/session, restore only its
captured backing, and never call `Browser.close`, terminate Chrome or clear user
cookies. A lost response is unconfirmed: it must be reconciled through status,
not converted into a successful task outcome or replayed automatically.

## Source locations

- [Connection adapter](../../../feral-core/skills/impl/existing_chrome.py):
  discovery, owned browser socket, flattened sessions and controller fences.
- [Connection manager](../../../feral-core/api/existing_chrome_connection.py) and
  [registered routes](../../../feral-core/api/routes/existing_chrome.py):
  consent, exact owner, selection and backing restoration.
- [ToolRunner](../../../feral-core/agents/tool_runner.py): captured resource
  admission and pending approval validation.
- [Native Browser feature](../../../desktop-native/NativeBrowserViewFeature.swift):
  connection/selection reviews and independent viewing controls.
- [Adapter fixtures](../../../feral-core/tests/test_existing_chrome_connection.py)
  and [route fixtures](../../../feral-core/tests/test_existing_chrome_routes.py).

## Verification

These are distinct checks, not additive product-readiness totals:

- Adapter fixtures: **52 passed**, one explicitly gated owned-Chrome case
  skipped, 1.42 seconds. They exercise the real adapter with controlled WebSocket replies,
  version/discovery validation, selected-session routing, failure/revocation,
  central resource admission and masked capture. This does not establish Chrome's
  personal-profile permission dialog or account behavior. The recorded command,
  from `feral-core`, is:

  ```sh
  env FERAL_HOME=/private/tmp/feral-existing-chrome-tests-home FERAL_DATA_HOME=/private/tmp/feral-existing-chrome-tests-home/data ../.venv/bin/python -m pytest tests/test_existing_chrome_connection.py -q --no-cov -p no:randomly --timeout=30
  ```

  This result was captured in tool stdout; no separate log-file receipt was
  retained. The explicit `FERAL_HOME` isolates the runtime; this command does not
  establish independent `FERAL_DATA_HOME` storage support.
- Registered route/manager fixtures: **43 passed**, zero skips, 1.26 seconds.
  Actual route registration, `bind_context` and ToolRunner resource capture are
  used; only the external connector boundary is controlled. Failure/cancellation,
  stale/foreign IDs, strict local admission, queued revocation, backing restoration
  and runtime/orchestrator/runner replacement are covered.
  Private receipt: `/private/tmp/feral-chrome-disconnect-race-fiw59czv/receipt.json`;
  log: `/private/tmp/feral-chrome-disconnect-race-fiw59czv/pytest.log`.
  Four tested inputs were unchanged; test SHA
  `d4976eb1ea5360aac83d7f2e9d1d1810892074bbc03425ae0ee7a5b66f2f9842`.
- Native Browser review/view fixtures: **103 checks passed** in the parent
  integration runner (`bash test_features.sh BrowserView Oversight`, from
  `desktop-native`). The same log records 12 Oversight groups, 28 linked wire
  groups, 39 desktop assertions and five error assertions. Log:
  `/private/tmp/feral-native-938-browser-oversight-final-tests.log`. Compiled synthetic
  responses establish UI review/parsing behavior, not personal Chrome access or
  a merchant/account outcome.
- Separate real installed-Chrome selector marker acceptance passed against a
  fresh synthetic profile. It validates the ordinary Playwright/controller
  path, not this existing-default-profile connection. See
  [browser observation evidence](BROWSER_VIEW_EVIDENCE.md).

Final frozen wider integration: **1,468 passed**, three opt-in skips, 230 warnings,
43 seconds across 53 suites. All 1,305 Python inputs remained unchanged; receipt:
`/private/tmp/feral-chrome-native-938-integration-20261004-tests-4did79mi/receipt.json`.
Ruff passes. Full mypy reports 809 diagnostics in 233 files, with no normalized
additions/removals against the preceding baseline; type-cleanliness is not claimed.
The initial five added diagnostics were corrected without raising the baseline.
Historical failed restoration/status cases and sandbox localhost-bind refusal
are retained. Native expiry verification now awaits the acknowledged cleanup
terminal state rather than racing local media retirement.

Actual installed Chrome direct-adapter acceptance passed **nine checks** on a
fresh owned profile, with the production default-path constructor addressed to an
isolated subprocess home and an owned endpoint file. It verifies exact-field
Unicode, wrong-focus preservation, accessibility snapshot, actual selector click,
password-mask/control pixels and socket-only disconnect. Receipt:
`/private/tmp/feral-browser-937-chrome-cshlme23/existing-chrome-real-receipt.json`.
The final annotated adapter hash is
`fa18a52a545c86e4cfc2552a0c775ee74f828acc24d2c600a4b334b801bab821`.
Owned Chrome exited normally; both listeners closed. The earlier gated real
adapter test also passed one case; counts overlap and its source preceded two
annotation-only corrections. Initial private helper/pre-readiness and expired
lease attempts are retained and do not count as successful tests.

Native selection now requires the exact reviewed target and a fresh connection
UUID in its success response. Unconfirmed refresh or attachment disables new
connection/attachment reviews until confirmed status; the captured disconnect
identity remains available for cleanup. Disconnect restoration also checks the
exact captured orchestrator/runner, so it cannot overwrite a replacement runtime.

Remaining acceptance: personal default-profile Chrome permission behavior,
visible task takeover, actual packaged native connection/selection/disconnect,
and approved task outcomes on representative applications. Login, 2FA, account
writes, purchases and payments have not been executed or verified by these tests.
No personal browser configuration or account was changed.
