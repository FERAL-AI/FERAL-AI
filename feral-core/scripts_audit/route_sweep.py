"""Ask a live brain whether every registered GET route still answers.

Run by hand:

    cd feral-core
    FERAL_HOME=/tmp/sweep python scripts_audit/route_sweep.py

Exits non-zero and prints the offenders if any authenticated read
returns 5xx or raises. ``tests/test_every_route_answers.py`` runs this
in a subprocess so a hanging or crashing route cannot take the whole
pytest session with it.

Why this exists
===============
Every defect in this codebase gets found the same way: by starting the
real thing and reading the output. The suites that run on every commit
structurally cannot see this class. pytest runs against mocks that
satisfy any attribute, vitest runs in jsdom which has no layout, and
``make e2e`` stubs ``**/api/**`` so it cannot notice a route that 500s.

The concrete case: ``/api/dashboard``, the endpoint the entire shell
polls, returned 500 for a whole release because
``getattr(state, "started_at", default)`` was called on a MagicMock,
and a MagicMock has every attribute so the default never applied. Every
unit test passed. The dashboard was blank.

The assertion is deliberately narrow: not "the body is right", which
needs a fixture per route and rots, but "an authenticated read does not
raise and does not 5xx". Cheap, total, unambiguous, and it is exactly
what reaches an operator as a blank panel.

GET only. Nothing here mutates the brain.
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

# Boot the way the brain really boots.
#
# The real entrypoint is `python -m api.server` run FROM feral-core, so
# sys.path[0] is feral-core and top-level packages like `migrations`
# resolve. Running a script out of a subdirectory puts THAT directory on
# sys.path instead, so `migrations` was not importable and the swept
# brain skipped its migration pass with a traceback in the log. A sweep
# that boots differently from production is measuring a different brain.
_FERAL_CORE = Path(__file__).resolve().parents[1]
if str(_FERAL_CORE) not in sys.path:
    sys.path.insert(0, str(_FERAL_CORE))


#: Paths this sweep cannot treat as a plain read, each with the reason.
#: The reason is not decoration: without one this list becomes the place
#: failures go to hide.
EXCLUDED: dict[str, str] = {
    "/stream": "server-sent events; deliberately holds the connection open",
    "/events": "server-sent events; deliberately holds the connection open",
    "/sse": "server-sent events; deliberately holds the connection open",
    "/ws": "websocket upgrade, not a GET with a body",
    "/shutdown": "stops the brain being swept",
    "/restart": "restarts the brain being swept",
    "/logs/tail": "follows a file and does not return",
    "/download": "streams a file body",
    "/export": "streams a file body",
}

#: A single route may not exceed this. Long enough for a cold cache,
#: short enough that a wedged route is reported rather than waited on.
PER_ROUTE_TIMEOUT_S = 15


def excluded_reason(path: str) -> str:
    for needle, reason in EXCLUDED.items():
        if needle in path:
            return reason
    return ""


def collect_paths(app) -> list[str]:
    paths = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "GET" not in methods or not path.startswith("/api/"):
            continue
        # A templated path needs a real id; a made-up one proves nothing.
        if "{" in path:
            continue
        if excluded_reason(path):
            continue
        paths.append(path)
    return sorted(set(paths))


class _Timeout(Exception):
    pass


def main() -> int:
    if not os.environ.get("FERAL_HOME"):
        print("refusing to run without FERAL_HOME set to a throwaway directory")
        return 2

    from fastapi.testclient import TestClient

    import api.server as srv

    paths = collect_paths(srv.app)
    if len(paths) < 50:
        print(f"only collected {len(paths)} routes; collection looks broken")
        return 2

    def _alarm(_signum, _frame):
        raise _Timeout()

    signal.signal(signal.SIGALRM, _alarm)

    failures: list[str] = []
    slow: list[str] = []
    ok = 0

    client = TestClient(srv.app, raise_server_exceptions=False)
    with client:
        client.post("/api/setup/complete", json={
            "settings": {}, "credentials": {}, "identity": {},
        })
        key_path = os.path.join(os.environ["FERAL_HOME"], "api_key")
        key = open(key_path).read().strip() if os.path.exists(key_path) else ""
        headers = {"Authorization": f"Bearer {key}"} if key else {}

        for path in paths:
            signal.alarm(PER_ROUTE_TIMEOUT_S)
            try:
                response = client.get(path, headers=headers)
                if response.status_code >= 500:
                    body = response.text[:200].replace("\n", " ")
                    failures.append(f"{path}: {response.status_code} {body}")
                else:
                    ok += 1
            except _Timeout:
                slow.append(path)
            except Exception as exc:  # noqa: BLE001 - report, keep sweeping
                failures.append(f"{path}: raised {exc.__class__.__name__}: {exc}")
            finally:
                signal.alarm(0)

    print(json.dumps({
        "routes": len(paths),
        "answered": ok,
        "failed": len(failures),
        "timed_out": len(slow),
    }, indent=2))
    for f in failures:
        print("  FAIL", f)
    for s in slow:
        print(f"  SLOW {s} (over {PER_ROUTE_TIMEOUT_S}s)")

    return 1 if (failures or slow) else 0


if __name__ == "__main__":
    sys.exit(main())
