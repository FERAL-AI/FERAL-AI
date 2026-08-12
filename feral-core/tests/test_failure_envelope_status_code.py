"""A failed skill call must not be stamped HTTP 200.

``SkillExecutor._execute_inner`` normalised every backing-implementation
result with ``result.get("status_code", 200)``. Ten integration modules
return ``{"success": False, "error": "Unknown endpoint: ..."}`` with no
status_code at all, so the envelope that reached the model, the SDUI
generator and the audit trail said 200.

The live store has it recorded verbatim, twice, for
``calendar_google__upcoming_events`` on 2026-05-21::

    {"success": false, "status_code": 200, "data": null,
     "error": "Unknown endpoint: upcoming_events"}

That endpoint had been renamed to ``list_events`` in v2026.5.38, so the
call was a real, diagnosable failure wearing a success-shaped code.
"""

from __future__ import annotations

import asyncio
import glob
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest  # noqa: E402
from skills.executor import SkillExecutor  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


def _manifest():
    endpoint = SkillEndpoint(
        id="upcoming_events", method="PYTHON", url="python://calendar",
        description="events",
    )
    manifest = SkillManifest(
        skill_id="calendar_google", version="1.0.0", description="calendar",
        brand=BrandProfile(name="Calendar"), endpoints=[endpoint],
    )
    return manifest, endpoint


class _Impl:
    def __init__(self, result):
        self._result = result

    async def execute(self, endpoint_id, args, vault):
        return self._result


def _run(impl_result, monkeypatch):
    manifest, endpoint = _manifest()
    ex = SkillExecutor()
    monkeypatch.setattr(
        "skills.impl.get_implementation", lambda skill_id: _Impl(impl_result),
    )
    return asyncio.run(ex._execute_inner(
        "calendar_google__upcoming_events", {}, manifest, endpoint,
    ))


def test_a_failure_without_a_status_code_is_not_200(monkeypatch):
    out = _run({"success": False, "error": "Unknown endpoint: upcoming_events"}, monkeypatch)
    assert out["success"] is False
    assert out["status_code"] != 200
    assert out["status_code"] == 500


def test_an_explicit_status_code_on_a_failure_is_preserved(monkeypatch):
    out = _run(
        {"success": False, "status_code": 404, "error": "no such calendar"},
        monkeypatch,
    )
    assert out["status_code"] == 404


def test_a_success_without_a_status_code_is_still_200(monkeypatch):
    out = _run({"success": True, "data": {"events": []}}, monkeypatch)
    assert out["success"] is True
    assert out["status_code"] == 200


def test_an_explicit_status_code_on_a_success_is_preserved(monkeypatch):
    out = _run({"success": True, "status_code": 201, "data": {}}, monkeypatch)
    assert out["status_code"] == 201


# ---------------------------------------------------------------------------
# The siblings that made this reachable
# ---------------------------------------------------------------------------

_INTEGRATIONS = [
    "calendar", "spotify", "notion", "email", "microsoft365",
    "home_assistant", "google_drive", "google_contacts", "messaging",
]


@pytest.mark.parametrize("module", _INTEGRATIONS)
def test_integrations_still_return_failures_without_a_status_code(module):
    """Documents why the default matters rather than asserting a style.

    These modules return ``{"success": False, "error": ...}`` with no
    status_code. That is fine now: the executor supplies 500. If one of
    them starts supplying its own code this test can be dropped for it.
    """
    path = os.path.join(REPO, "integrations", f"{module}.py")
    src = open(path, encoding="utf-8").read()
    assert re.search(r'\{"success": False, "error": f?"Unknown endpoint', src), (
        f"{module}.py no longer matches the shape this default covers"
    )


def test_the_shape_is_present_in_the_tree():
    hits = 0
    for path in glob.glob(os.path.join(REPO, "integrations", "*.py")):
        src = open(path, encoding="utf-8").read()
        if re.search(r'\{"success": False, "error": f?"Unknown endpoint', src):
            hits += 1
    assert hits >= 9, f"expected the audited shape in >=9 integrations, found {hits}"
