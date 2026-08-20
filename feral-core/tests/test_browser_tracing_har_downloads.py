"""PR 7 gap-fill (browser): tracing / HAR / download primitives on
BrowserController.

We can't realistically spin up Playwright in CI — but we can pin:

* Methods exist and have stable signatures.
* When Playwright isn't connected, the API returns a *truthful* error
  string with a remediation hint (instead of pretending success).
* The brain's manifest alias map exposes the new endpoint ids.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run(coro):
    # Use asyncio.run for Python 3.11+ compatibility (no auto loop in MainThread).
    return asyncio.run(coro)


def test_controller_exposes_tracing_har_download_methods():
    from skills.impl.browser_use import BrowserController

    bc = BrowserController()
    for name in ("start_tracing", "stop_tracing", "start_har", "stop_har", "wait_for_download"):
        assert callable(getattr(bc, name, None)), f"BrowserController missing {name}"


def test_methods_report_truthfully_when_playwright_not_connected():
    from skills.impl.browser_use import BrowserController

    bc = BrowserController()
    # No Playwright initialise() ran, so _page / _browser are None.
    out = _run(bc.start_tracing())
    assert out["success"] is False
    assert "Playwright" in out["error"]

    out = _run(bc.start_har())
    assert out["success"] is False
    assert "Playwright" in out["error"]

    out = _run(bc.wait_for_download(timeout_ms=50))
    assert out["success"] is False
    assert "Playwright" in out["error"]


def test_stop_tracing_without_start_returns_error():
    from skills.impl.browser_use import BrowserController

    bc = BrowserController()
    out = _run(bc.stop_tracing())
    assert out["success"] is False
    assert "No active tracing" in out["error"]


# PR-stack note: the alias-map presence test (trace_start, har_start,
# download_next, ...) is intentionally NOT in this PR. The aliases are
# wired in api/state.py by PR 11 (which lands the BrainState integration
# wiring); the PR-11 branch carries `tests/test_brain_browser_alias_map.py`
# that pins them. Keeping this PR focused on controller methods avoids
# a cross-PR import dependency.


def test_artifacts_directory_is_namespaced_under_feral_home():
    """Tracing/HAR/download artefacts live under the FERAL data home.

    Assertion updated from a literal ``~/.feral`` suffix to the resolver:
    ``_artifacts_root`` built ``Path.home() / ".feral"`` by hand and so
    ignored ``FERAL_HOME`` entirely, which meant an isolated run still
    wrote into the operator's real home directory. The old assertion
    passed either way, which is why it never caught it.
    """
    from config.loader import feral_data_home
    from skills.impl.browser_use import BrowserController

    bc = BrowserController()
    p = bc._artifacts_root
    assert p == feral_data_home() / "browser" / "artifacts", p
    assert p.exists() and p.is_dir()
