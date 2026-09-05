"""Prerequisite gate for the tool list the model is offered each turn.

Measured on the operator's brain, 2026-09-04: of the 266 tool schemas
handed to the model on every turn, **79 could not have worked**. Not
"might fail", could not: Docker was never installed so the boot log
already said "DockerSandbox constructed but is not functional, code
execution skill disabled"; no GitHub token was stored; Google, Notion
and Microsoft were never authorised; no CuteBot was plugged into the
USB port. The model was shown all 79 anyway, picked them, called them,
and got a deterministic failure back. That is what the operator meant
by "the tools are not working".

The 79 break down by skill exactly as follows, and this table is the
whole configuration of this module:

===================  ============================
sandbox              code_interpreter (2)
device               cutebot (6)
API key absent       github_api (6), image_gen (1)
not connected        calendar_google (6), email (7),
                     google_contacts (3), google_drive (5),
                     messaging_sms (9), microsoft365 (6),
                     notion (6), smart_home_hue (12),
                     spotify_music (10)
===================  ============================

Three rules govern every check here.

**Cheap.** This runs while a turn is being assembled, so nothing may
block. Every check reads process memory: an integration's ``connected``
property is the in-process probe cache (``integrations/_probe_status``),
``OAuthManager.is_connected`` is a dict lookup, the device registry is a
dict. The one exception is Docker, and it is why ``DockerSandbox.available()``
is deliberately NOT called: it shells ``docker info`` with a 15 second
timeout. ``shutil.which("docker")`` answers the question the boot log
actually asked ("is Docker installed") without the subprocess.

**Network-free.** No check may dial anything. A gate that spends five
seconds on a DNS lookup to decide whether to offer a tool is worse than
the dead tool it replaces.

**Fail open.** Anything this module cannot resolve, for any reason,
counts as AVAILABLE. A brain that is still booting, a unit test with no
``BrainState``, an integration object that raises from its own
``connected`` property: all of these leave the tool offered. Hiding a
tool that would have worked is a worse failure than offering one that
would not, because the model cannot route around a capability it was
never shown.

The tools that are withheld are not hidden from the model, only from its
tool list: :func:`availability_note` returns one line for the system
prompt naming what is off and why, so it can tell the user "Email is not
connected" instead of calling and failing.

``FERAL_OFFER_UNAVAILABLE_TOOLS=1`` restores the old behaviour wholesale.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("feral.skills.availability")

#: Escape hatch. Set to 1/true/yes to offer every registered tool again,
#: prerequisites be damned. Exists so an operator debugging a wrong
#: verdict from this module can get the old surface back without a patch.
OFFER_UNAVAILABLE_ENV = "FERAL_OFFER_UNAVAILABLE_TOOLS"

#: Verdicts are recomputed at most this often. Connecting an integration
#: or plugging in the robot therefore takes effect within a turn or two,
#: which is the right trade for keeping every turn's assembly free.
CACHE_TTL_SECONDS = 30.0

KIND_INTEGRATION = "integration"
KIND_API_KEY = "api_key"
KIND_SANDBOX = "sandbox"
KIND_DEVICE = "device"


@dataclass(frozen=True)
class Prerequisite:
    """One skill's precondition, and what to say when it is not met.

    ``label`` and ``reason`` are written to read as a sentence in the
    system-prompt note: "Email: not connected."
    """

    skill_id: str
    kind: str
    label: str
    reason: str
    #: ``BrainState`` attribute holding the integration whose ``connected``
    #: property answers the question (``kind == KIND_INTEGRATION``).
    state_attr: str = ""
    #: Env vars the skill's own implementation falls back to. Consulted
    #: because ``SkillExecutor._get_key`` reads the vault and its
    #: lower-cased in-process cache only, while implementations also read
    #: bare provider env vars (``weather`` reads ``OPENWEATHER_API_KEY``,
    #: ``web_search`` reads ``TAVILY_API_KEY``). Asking only the executor
    #: would declare a working skill dead.
    env_keys: tuple[str, ...] = ()
    #: Device id the hardware registry must know (``kind == KIND_DEVICE``).
    device_id: str = ""


#: Deliberately NOT in this table: ``workspace_scripts``. Two of its four
#: endpoints run generated code and do need Docker, but ``list_catalog``
#: and ``delete`` only touch a local JSON file and work fine without it
#: (see ``SkillExecutor._is_sandbox_required``). Gating the skill would
#: take away an operator's ability to list their own saved scripts, so
#: this stays skill-granular and leaves that one alone.
PREREQUISITES: tuple[Prerequisite, ...] = (
    Prerequisite(
        "code_interpreter", KIND_SANDBOX,
        "Code execution", "Docker not installed",
    ),
    Prerequisite(
        "cutebot", KIND_DEVICE,
        "CuteBot", "not plugged in", device_id="cutebot-usb-0",
    ),
    Prerequisite(
        "github_api", KIND_API_KEY,
        "GitHub", "no API token",
        env_keys=("GITHUB_TOKEN", "GH_TOKEN"),
    ),
    Prerequisite(
        "image_gen", KIND_API_KEY,
        "Image generation", "no API key",
        env_keys=("OPENAI_API_KEY",),
    ),
    Prerequisite(
        "calendar_google", KIND_INTEGRATION,
        "Calendar", "not connected", state_attr="calendar",
    ),
    Prerequisite(
        "email", KIND_INTEGRATION,
        "Email", "not connected", state_attr="email",
    ),
    Prerequisite(
        "google_contacts", KIND_INTEGRATION,
        "Google Contacts", "not connected", state_attr="google_contacts",
    ),
    Prerequisite(
        "google_drive", KIND_INTEGRATION,
        "Google Drive", "not connected", state_attr="google_drive",
    ),
    Prerequisite(
        "messaging_sms", KIND_INTEGRATION,
        "Messaging", "not connected", state_attr="messaging",
    ),
    Prerequisite(
        "microsoft365", KIND_INTEGRATION,
        "Microsoft 365", "not connected", state_attr="microsoft365",
    ),
    Prerequisite(
        "notion", KIND_INTEGRATION,
        "Notion", "not connected", state_attr="notion",
    ),
    Prerequisite(
        "smart_home_hue", KIND_INTEGRATION,
        "Smart home", "not connected", state_attr="home_assistant",
    ),
    Prerequisite(
        "spotify_music", KIND_INTEGRATION,
        "Spotify", "not connected", state_attr="spotify",
    ),
)

PREREQUISITES_BY_SKILL: dict[str, Prerequisite] = {
    p.skill_id: p for p in PREREQUISITES
}


def offering_unavailable_tools() -> bool:
    """Is the escape hatch set?"""
    return (os.environ.get(OFFER_UNAVAILABLE_ENV, "") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _brain_state() -> Any:
    """The live ``BrainState``, or ``None`` before boot / in unit tests."""
    try:
        import api.state as state_module
    except Exception:
        return None
    return getattr(state_module, "state", None)


def _integration_connected(prereq: Prerequisite, state: Any) -> Optional[bool]:
    """``True``/``False`` from the integration's own ``connected``, or
    ``None`` when there is nothing to ask (fail open)."""
    if state is None or not prereq.state_attr:
        return None
    integration = getattr(state, prereq.state_attr, None)
    if integration is None:
        return None
    try:
        return bool(integration.connected)
    except Exception as exc:
        logger.debug(
            "availability: %s.connected raised (%s); treating as available",
            prereq.state_attr, exc,
        )
        return None


def _api_key_present(prereq: Prerequisite, state: Any) -> Optional[bool]:
    """Is a key resolvable for this skill, from the vault OR the env vars
    the implementation itself falls back to?"""
    for env_key in prereq.env_keys:
        if (os.environ.get(env_key, "") or "").strip():
            return True
    executor = getattr(state, "skill_executor", None) if state is not None else None
    if executor is None:
        # Nothing authoritative to ask. Fail open rather than guess.
        return None
    getter = getattr(executor, "_get_key", None)
    if not callable(getter):
        return None
    try:
        return bool(getter(prereq.skill_id))
    except Exception as exc:
        logger.debug(
            "availability: key lookup for %s raised (%s); treating as available",
            prereq.skill_id, exc,
        )
        return None


def _sandbox_present() -> Optional[bool]:
    """Is Docker installed?

    ``shutil.which`` and nothing else, on purpose. The obvious call,
    ``DockerSandbox.available()``, runs ``docker info`` in a subprocess
    with a 15 second timeout, which is not something a per-turn gate may
    do. A machine with the CLI installed but the daemon stopped keeps its
    tools offered; that is the fail-open direction, and the executor
    still returns its explicit "start Docker Desktop" 503.
    """
    try:
        return shutil.which("docker") is not None
    except Exception:
        return None


def _device_present(prereq: Prerequisite, state: Any) -> Optional[bool]:
    """Does the hardware registry know this device?

    Mirrors ``CuteBotSkill._is_device_registered`` exactly, including the
    ``_devices`` fallback, so the gate and the skill cannot disagree
    about whether the robot is there.
    """
    if state is None or not prereq.device_id:
        return None
    registry = getattr(state, "device_registry", None)
    if registry is None:
        return None
    try:
        if hasattr(registry, "get_device"):
            return registry.get_device(prereq.device_id) is not None
        devices = getattr(registry, "_devices", None)
        if isinstance(devices, dict):
            return prereq.device_id in devices
    except Exception as exc:
        logger.debug(
            "availability: device lookup for %s raised (%s); treating as available",
            prereq.device_id, exc,
        )
    return None


def _evaluate(prereq: Prerequisite, state: Any) -> Optional[bool]:
    """``True`` available, ``False`` unavailable, ``None`` unknown."""
    if prereq.kind == KIND_INTEGRATION:
        return _integration_connected(prereq, state)
    if prereq.kind == KIND_API_KEY:
        return _api_key_present(prereq, state)
    if prereq.kind == KIND_SANDBOX:
        return _sandbox_present()
    if prereq.kind == KIND_DEVICE:
        return _device_present(prereq, state)
    return None


_cache: dict[str, str] = {}
_cache_at: float = 0.0


def invalidate() -> None:
    """Drop the memoised verdicts. Tests, and anything that has just
    connected an integration and wants the next turn to know."""
    global _cache_at
    _cache_at = 0.0
    _cache.clear()


def unavailable_skills(*, force: bool = False) -> dict[str, str]:
    """Map ``skill_id -> "Label: reason"`` for every skill whose
    prerequisite is provably absent.

    Memoised for :data:`CACHE_TTL_SECONDS`. Empty when the escape hatch
    is set, so a single env var restores the pre-gate tool surface.
    """
    global _cache_at
    if offering_unavailable_tools():
        return {}
    now = time.monotonic()
    if not force and _cache_at and (now - _cache_at) < CACHE_TTL_SECONDS:
        return dict(_cache)

    state = _brain_state()
    verdicts: dict[str, str] = {}
    for prereq in PREREQUISITES:
        if _evaluate(prereq, state) is False:
            verdicts[prereq.skill_id] = f"{prereq.label}: {prereq.reason}"

    if verdicts != _cache:
        logger.info(
            "Tool availability: withholding %d skill(s) whose prerequisites "
            "are absent (%s)",
            len(verdicts), ", ".join(sorted(verdicts)) or "none",
        )
    _cache.clear()
    _cache.update(verdicts)
    _cache_at = now
    return dict(verdicts)


def filter_unavailable_tools(
    tools: list[dict],
    unavailable: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Drop every tool belonging to a skill whose prerequisite is absent.

    Tool names are ``skill__endpoint``; anything that does not parse that
    way (MCP tools, built-ins) is kept untouched.
    """
    if not tools:
        return list(tools or [])
    if unavailable is None:
        unavailable = unavailable_skills()
    if not unavailable:
        return list(tools)

    from agents.tool_list import skill_id_from_tool_name, tool_name_from_def

    kept: list[dict] = []
    dropped = 0
    for tool in tools:
        skill_id = skill_id_from_tool_name(tool_name_from_def(tool))
        if skill_id and skill_id in unavailable:
            dropped += 1
            continue
        kept.append(tool)
    if dropped:
        logger.debug(
            "availability gate: %d of %d tool schemas withheld (%s)",
            dropped, len(tools), ", ".join(sorted(unavailable)),
        )
    return kept


def availability_note(unavailable: Optional[dict[str, str]] = None) -> str:
    """One line for the system prompt naming what is off and why, or "".

    The point is honesty, not apology. Without it the model sees a
    capability vanish from its tool list and has no way to tell "FERAL
    cannot do this" from "this needs connecting", so it guesses, and the
    guess it reaches for is the wrong one.
    """
    if unavailable is None:
        unavailable = unavailable_skills()
    if not unavailable:
        return ""
    reasons = ". ".join(sorted(unavailable.values()))
    return (
        "Not available right now (no tools offered for these, do not "
        f"claim FERAL lacks the capability): {reasons}. "
        "If the user asks for one, say what is missing and how to fix it."
    )
