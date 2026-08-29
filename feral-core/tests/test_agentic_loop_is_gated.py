"""The agentic loop's inner actions must pass the same gates as any tool.

`SkillExecutor.execute` is the chokepoint. Its own docstring explains
why the gates were moved there: plan mode and approval used to live only
in `ToolRunner`, seven production callers reach the executor directly,
and "a gate the caller has to opt into fails open for every path that
does not know to ask". Two such bypasses had already been patched
individually before anyone counted the rest.

`agentic_computer_use._dispatch_via_gui` was another one. It called
`get_implementation("gui_computer_use").execute(...)` on the raw skill
instance, so `_gate` (plan mode, approval) and `_rate_limit` never ran
for any action inside the loop.

The consequence is not subtle. One approved `execute_task` call bought
up to fifteen VLM iterations of clicking and typing that were never
gated and never counted against the hourly budget, and plan mode, whose
entire promise is that nothing acts, did not stop them.

This is the layer the operator's autonomy tier is supposed to own. A
tier that governs the outer call and not the fifteen actions it spawns
is not governing much.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.impl.agentic_computer_use import AgenticComputerUseSkill  # noqa: E402


class _Action:
    """Smallest shape `_dispatch_via_gui` reads."""

    def __init__(self, action="click", x=10, y=20):
        self.action = action
        self.x = x
        self.y = y
        self.text = ""
        self.keys = ""
        self.direction = "down"
        self.amount = 3
        self.reasoning = ""


@pytest.fixture()
def skill():
    return AgenticComputerUseSkill()


class _Endpoint:
    def __init__(self, ep_id):
        self.id = ep_id


class _Manifest:
    """Only the two attributes the resolver reads."""

    skill_id = "gui_computer_use"

    def __init__(self):
        # Built from the real action->endpoint map rather than a hand
        # list, which was wrong: "click" dispatches to "mouse_click".
        # A hand-written fixture that drifts from the map makes the
        # resolver fail, which falls back to the direct path, which
        # makes the gating test pass for the wrong reason.
        from agents.computer_use_driver import GUI_ENDPOINT_FOR
        self.endpoints = [_Endpoint(e) for e in set(GUI_ENDPOINT_FOR.values())]


def _wire_state(monkeypatch, *, executor=None, gui=None):
    """Install a fake `api.state` the dispatcher can find.

    The registry has to resolve for real: if `_resolve_gui_endpoint`
    cannot find the manifest it falls back to the direct path by design,
    so a MagicMock registry would make this test pass for the wrong
    reason and prove nothing about gating.
    """
    state_mod = MagicMock()
    state_obj = MagicMock()
    state_obj.skill_executor = executor
    registry = MagicMock()
    registry.skills = {"gui_computer_use": _Manifest()}
    state_obj.skill_registry = registry
    state_mod.state = state_obj
    monkeypatch.setitem(sys.modules, "api.state", state_mod)

    if gui is not None:
        import skills.impl as impl_pkg
        monkeypatch.setattr(
            impl_pkg, "get_implementation", lambda name: gui, raising=False,
        )
    return state_obj


@pytest.mark.asyncio
async def test_inner_actions_go_through_the_skill_executor(skill, monkeypatch):
    """Not the raw skill instance, which skips every gate."""
    executor = MagicMock()
    executor.execute = AsyncMock(return_value={
        "success": True, "status_code": 200, "data": {"message": "clicked"},
    })
    raw_gui = MagicMock()
    raw_gui.execute = AsyncMock(return_value={"success": True, "data": {}})
    _wire_state(monkeypatch, executor=executor, gui=raw_gui)

    await skill._dispatch_via_gui(_Action())

    assert executor.execute.await_count == 1, (
        "the inner action did not go through SkillExecutor, so plan mode, "
        "approval and the hourly rate limit were all skipped"
    )
    assert raw_gui.execute.await_count == 0, (
        "the inner action still called the raw skill instance, bypassing the "
        "chokepoint"
    )


@pytest.mark.asyncio
async def test_a_refused_inner_action_is_not_executed(skill, monkeypatch):
    """Plan mode's whole promise is that nothing acts."""
    refusal = {
        "success": False, "status_code": 403,
        "data": None, "error": "plan mode: no actions",
    }
    executor = MagicMock()
    executor.execute = AsyncMock(return_value=refusal)
    raw_gui = MagicMock()
    raw_gui.execute = AsyncMock(return_value={"success": True, "data": {}})
    _wire_state(monkeypatch, executor=executor, gui=raw_gui)

    message = await skill._dispatch_via_gui(_Action())

    assert raw_gui.execute.await_count == 0, "a refused action still ran"
    assert "plan mode" in str(message).lower() or "failed" in str(message).lower(), (
        f"the refusal was not surfaced to the loop: {message!r}"
    )


@pytest.mark.asyncio
async def test_it_still_works_with_no_brain_attached(skill, monkeypatch):
    """Offline tooling, tests and the CLI have no `api.state`.

    `SkillExecutor._gate` fails open for exactly this reason and says so.
    Refusing here would break those callers without making a live
    session any safer, so the direct path remains the fallback, not the
    default.
    """
    monkeypatch.delitem(sys.modules, "api.state", raising=False)
    raw_gui = MagicMock()
    raw_gui.execute = AsyncMock(return_value={
        "success": True, "status_code": 200, "data": {"message": "clicked"},
    })
    import skills.impl as impl_pkg
    monkeypatch.setattr(
        impl_pkg, "get_implementation", lambda name: raw_gui, raising=False,
    )

    message = await skill._dispatch_via_gui(_Action())

    assert raw_gui.execute.await_count == 1, (
        "with no brain attached the dispatcher should still act"
    )
    assert "clicked" in str(message)


@pytest.mark.asyncio
async def test_a_missing_gui_skill_is_reported_not_crashed(skill, monkeypatch):
    monkeypatch.delitem(sys.modules, "api.state", raising=False)
    import skills.impl as impl_pkg
    monkeypatch.setattr(
        impl_pkg, "get_implementation", lambda name: None, raising=False,
    )
    message = await skill._dispatch_via_gui(_Action())
    assert "not registered" in str(message)


def test_the_dispatcher_does_not_reach_for_the_raw_instance_first():
    """Pin the ordering: executor first, raw instance only as fallback."""
    src = (ROOT / "skills" / "impl" / "agentic_computer_use.py").read_text()
    assert "skill_executor" in src, (
        "agentic_computer_use never mentions skill_executor; its inner loop "
        "is still ungated"
    )
