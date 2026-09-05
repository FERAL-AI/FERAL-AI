"""Dead tools are not offered, and the model is told why.

Evidence these tests are written against, from the read-only audit of the
operator's brain on 2026-09-04:

* 266 tool schemas were offered on every turn and 79 of them could not
  have worked. Docker was not installed (the boot log says so), no GitHub
  token was stored, Google/Notion/Microsoft were never authorised, and no
  CuteBot was plugged in.
* The Responses-API builder applied no tool cap at all, so gpt-5.6-sol
  received all 266 schemas: 32,391 tokens, 97% of every request.

One case per prerequisite class, one for the system-prompt note, one for
the escape hatch, and one asserting the Responses body is capped.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.llm_provider import LLMProvider  # noqa: E402
from agents.tool_list import OPENAI_TOOL_HARD_LIMIT  # noqa: E402
from skills import availability  # noqa: E402
from skills.availability import (  # noqa: E402
    OFFER_UNAVAILABLE_ENV,
    PREREQUISITES_BY_SKILL,
    availability_note,
    filter_unavailable_tools,
    unavailable_skills,
)
from skills.registry import SkillRegistry  # noqa: E402


class _FakeState:
    """Just enough BrainState for the gate to read."""

    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(self, key, value)


def _integration(connected: bool):
    obj = MagicMock()
    type(obj).connected = property(lambda _self: connected)
    return obj


def _device_registry(*device_ids: str):
    reg = MagicMock()
    reg.get_device = lambda did: object() if did in device_ids else None
    return reg


def _executor_with_keys(*skill_ids: str):
    ex = MagicMock()
    ex._get_key = lambda sid: "tok" if sid in skill_ids else ""
    return ex


@pytest.fixture(autouse=True)
def _clean_gate(monkeypatch):
    """No cached verdicts, no escape hatch, no live BrainState."""
    monkeypatch.delenv(OFFER_UNAVAILABLE_ENV, raising=False)
    availability.invalidate()
    yield
    availability.invalidate()


@pytest.fixture()
def state(monkeypatch):
    """Install a fake BrainState the gate will read, and return it.

    ``monkeypatch.setattr`` on the module attribute, not
    ``setitem(sys.modules, ...)``: ``import api.state as m`` resolves
    through ``getattr(api, "state")`` once the package is imported, so a
    replacement in ``sys.modules`` is simply not seen.
    """
    import api.state as state_module

    fake = _FakeState()
    monkeypatch.setattr(state_module, "state", fake)
    return fake


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


# ── One case per prerequisite class ──────────────────────────────────


def test_oauth_integration_that_is_not_connected_is_withheld(state, monkeypatch):
    """calendar_google, email, notion, google_drive, google_contacts,
    microsoft365, spotify_music, smart_home_hue and messaging_sms all
    answer through an integration's ``connected`` property."""
    state.calendar = _integration(False)
    state.email = _integration(True)
    monkeypatch.setattr(availability, "_sandbox_present", lambda: True)

    verdicts = unavailable_skills(force=True)

    assert "calendar_google" in verdicts
    assert verdicts["calendar_google"] == "Calendar: not connected"
    assert "email" not in verdicts, "a connected integration must stay offered"


def test_api_key_absent_is_withheld_and_env_fallback_counts(state, monkeypatch):
    """The gate consults the env vars the IMPLEMENTATION falls back to.

    ``SkillExecutor._get_key`` reads the vault and its lower-cased cache
    only, while skills also read bare provider env vars (weather reads
    OPENWEATHER_API_KEY, web_search reads TAVILY_API_KEY). Asking the
    executor alone would declare a working skill dead.
    """
    state.skill_executor = _executor_with_keys()  # vault holds nothing
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(availability, "_sandbox_present", lambda: True)

    assert "github_api" in unavailable_skills(force=True)

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_live")
    assert "github_api" not in unavailable_skills(force=True)


def test_api_key_in_the_vault_counts_even_with_no_env_var(state, monkeypatch):
    state.skill_executor = _executor_with_keys("image_gen")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(availability, "_sandbox_present", lambda: True)

    assert "image_gen" not in unavailable_skills(force=True)


def test_sandbox_absent_withholds_code_interpreter(state, monkeypatch):
    monkeypatch.setattr(availability, "_sandbox_present", lambda: False)

    verdicts = unavailable_skills(force=True)

    assert verdicts["code_interpreter"] == "Code execution: Docker not installed"


def test_sandbox_check_never_runs_docker_info(state, monkeypatch):
    """``DockerSandbox.available()`` shells ``docker info`` with a 15s
    timeout. A per-turn gate may not do that, so the check is
    ``shutil.which`` and nothing else."""
    calls = []
    monkeypatch.setattr(
        availability.shutil, "which",
        lambda name: calls.append(name) or None,
    )

    assert availability._sandbox_present() is False
    assert calls == ["docker"]


def test_absent_device_withholds_cutebot(state, monkeypatch):
    state.device_registry = _device_registry()  # nothing plugged in
    monkeypatch.setattr(availability, "_sandbox_present", lambda: True)

    assert unavailable_skills(force=True)["cutebot"] == "CuteBot: not plugged in"

    state.device_registry = _device_registry("cutebot-usb-0")
    assert "cutebot" not in unavailable_skills(force=True)


# ── Fail open ────────────────────────────────────────────────────────


def test_unknown_prerequisites_leave_every_tool_offered(monkeypatch):
    """No BrainState (still booting, or a unit test) hides nothing.

    Offering a tool that will fail is recoverable. Hiding one that would
    have worked is not: the model cannot route around a capability it was
    never shown.
    """
    import api.state as state_module

    monkeypatch.setattr(state_module, "state", None)
    monkeypatch.setattr(availability, "_sandbox_present", lambda: True)

    assert unavailable_skills(force=True) == {}


def test_an_integration_that_raises_is_treated_as_available(state, monkeypatch):
    broken = MagicMock()
    type(broken).connected = property(
        lambda _self: (_ for _ in ()).throw(RuntimeError("vault exploded"))
    )
    state.notion = broken
    monkeypatch.setattr(availability, "_sandbox_present", lambda: True)

    assert "notion" not in unavailable_skills(force=True)


# ── The system-prompt note ───────────────────────────────────────────


def test_note_names_each_withheld_capability_and_its_reason():
    note = availability_note({
        "email": "Email: not connected",
        "code_interpreter": "Code execution: Docker not installed",
        "cutebot": "CuteBot: not plugged in",
    })

    assert "Code execution: Docker not installed" in note
    assert "CuteBot: not plugged in" in note
    assert "Email: not connected" in note
    # The point is that the model does not report the capability missing.
    assert "do not claim feral lacks the capability" in note.lower()


def test_note_is_empty_when_everything_works():
    assert availability_note({}) == ""


def test_the_note_reaches_the_system_prompt(state, monkeypatch):
    """The withheld half has to be legible to the model, or it cannot
    tell "FERAL cannot do this" from "this needs connecting"."""
    from agents.self_model import build_core_self_model

    state.device_registry = _device_registry()
    monkeypatch.setattr(availability, "_sandbox_present", lambda: False)

    block = build_core_self_model(active_skills=[], full_skills=[])

    assert "CuteBot: not plugged in" in block
    assert "Code execution: Docker not installed" in block


def test_the_tooling_catalog_itself_stays_a_pure_function(state, monkeypatch):
    """``build_tooling_catalog`` is a function of its arguments and has an
    exact-output test; the note is appended by the prompt builders around
    it, not baked into it."""
    from agents.self_model import build_tooling_catalog

    state.device_registry = _device_registry()
    monkeypatch.setattr(availability, "_sandbox_present", lambda: False)

    assert build_tooling_catalog([], []) == (
        "## Tooling\n### Active this turn\n"
        "(none routed — rely on the always-include fallback set)"
    )


# ── The escape hatch ─────────────────────────────────────────────────


def test_escape_hatch_restores_the_old_surface(state, monkeypatch):
    state.device_registry = _device_registry()
    monkeypatch.setattr(availability, "_sandbox_present", lambda: False)
    assert unavailable_skills(force=True)

    monkeypatch.setenv(OFFER_UNAVAILABLE_ENV, "1")
    assert unavailable_skills(force=True) == {}
    assert availability_note() == ""


# ── The tool list actually shrinks ───────────────────────────────────


def test_filter_drops_the_withheld_skill_and_keeps_the_rest():
    tools = [
        _tool("cutebot__set_lights"),
        _tool("cutebot__drive"),
        _tool("coding_tools__read_file"),
        {"name": "mcp_server_thing"},
    ]

    kept = filter_unavailable_tools(tools, {"cutebot": "CuteBot: not plugged in"})

    names = [t.get("function", t).get("name") for t in kept]
    assert names == ["coding_tools__read_file", "mcp_server_thing"]


def test_the_registry_stays_the_full_inventory(state, monkeypatch):
    """``GET /api/tools``, the Skills page and the MCP projection all
    describe the INSTALLED surface. A skill the operator has not connected
    yet is still installed, so the gate lives in the callers that assemble
    a tool list for a model, never in the registry."""
    state.device_registry = _device_registry()
    monkeypatch.setattr(availability, "_sandbox_present", lambda: True)

    registry = SkillRegistry()
    registry.load_builtin_skills()

    full = registry.get_all_tools()
    offerable = filter_unavailable_tools(full)

    def _names(tools):
        return {t["function"]["name"] for t in tools}

    assert "cutebot__set_lights" in _names(full)
    assert "cutebot__set_lights" not in _names(offerable)
    # cutebot ships six endpoints and they all go.
    assert len(full) - len(offerable) == 6


def test_offerable_only_withholds_the_measured_79_when_nothing_is_set_up(
    state, monkeypatch,
):
    """The audit's headline number, reproduced against the shipped
    manifests: no key, no OAuth, no Docker, no robot."""
    monkeypatch.setattr(availability, "_sandbox_present", lambda: False)
    for env_key in ("GITHUB_TOKEN", "GH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(env_key, raising=False)
    state.skill_executor = _executor_with_keys()
    state.device_registry = _device_registry()
    for attr in ("calendar", "email", "google_contacts", "google_drive",
                 "messaging", "microsoft365", "notion", "home_assistant",
                 "spotify"):
        setattr(state, attr, _integration(False))

    registry = SkillRegistry()
    registry.load_builtin_skills()

    full = registry.get_all_tools()
    dropped = len(full) - len(filter_unavailable_tools(full))
    assert dropped == 79, (
        "the 13 skills in skills/availability.PREREQUISITES own exactly 79 "
        "of the shipped tool schemas"
    )
    assert set(unavailable_skills(force=True)) == set(PREREQUISITES_BY_SKILL)


# ── The Responses-API cap ────────────────────────────────────────────


def _oversized_tools(count: int) -> list[dict]:
    return [_tool(f"skill_{i}__endpoint") for i in range(count)]


def test_responses_body_is_capped_at_the_openai_tool_limit():
    """gpt-5.6-sol goes to /v1/responses, and that builder applied no cap
    at all: 266 schemas, 32,391 tokens, 97% of the request."""
    provider = LLMProvider.__new__(LLMProvider)
    provider.provider = "openai"
    provider.model = "gpt-5.6-sol"

    tools = _oversized_tools(266)
    body = provider._build_responses_body(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        temperature=1.0,
        max_tokens=256,
        stream=False,
    )

    assert len(body["tools"]) == OPENAI_TOOL_HARD_LIMIT
    assert len(tools) == 266, "the caller's list must not be mutated"


def test_responses_body_leaves_a_small_tool_list_alone():
    provider = LLMProvider.__new__(LLMProvider)
    provider.provider = "openai"
    provider.model = "gpt-5.6-sol"

    body = provider._build_responses_body(
        messages=[{"role": "user", "content": "hi"}],
        tools=_oversized_tools(9),
        temperature=1.0,
        max_tokens=256,
        stream=False,
    )

    assert len(body["tools"]) == 9
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["name"] == "skill_0__endpoint"
