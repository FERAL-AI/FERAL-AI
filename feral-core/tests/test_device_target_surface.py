"""Phase 1 (audit-r10 overhaul) regression tests — `device_target` field
+ ExecutionSurfacePolicy resolver.

Operator report 2026-05-13 (audit-r10 #4 + #8):
> "the chat on the app when I ask for something to be add on my desktop
>  ... it says that it has no access and then asks for approval, but
>  then it comes back and it asks to enable something from the settings
>  on my mac but nothing there and never works"

Root cause: `security/dangerous_tools.py` mapped `phone_surface` →
`http_api` unconditionally, hard-denying every Mac-side actuation tool
(`computer_use__bash`, `desktop_control__shell_command`,
`agentic_computer_use__execute_task`). The LLM saw the deny and
hallucinated "go to Settings" prose.

Phase 1 fix: introduce `device_target` ∈ {"brain", "phone", "glasses",
"auto", None} on `ChatRequestPayload` + `HUPActionRequestPayload`. The
resolver consults `device_target` first; only falls back to source→
surface for `auto` / None. New surfaces:
  - `brain_host` — operator's Mac, full desktop control allowed
  - `phone_actuator` — phone runs `phone.*` skills only (Phase 4)

These tests pin the wire contract + the resolver behavior so a future
refactor cannot silently regress to the old hard-deny.
"""

from __future__ import annotations

import pytest

from models.protocol import ChatRequestPayload, HUPActionRequestPayload
from security.dangerous_tools import (
    is_tool_allowed,
    known_surfaces,
    resolve_surface_from_context,
)


# ───────────────────────── wire contract ──────────────────────────


def test_chat_request_payload_accepts_device_target():
    """ChatRequestPayload gains a `device_target` field. None default
    preserves backward compatibility with older clients that don't
    send it."""
    p = ChatRequestPayload(session_id="s", text="hi")
    assert p.device_target is None

    for target in ("brain", "phone", "glasses", "auto"):
        p = ChatRequestPayload(session_id="s", text="hi", device_target=target)
        assert p.device_target == target


def test_chat_request_payload_rejects_unknown_device_target():
    """Unknown values must fail validation — the wire enum is the
    source of truth so the orchestrator never sees `device_target ==
    "wristband"` and silently does nothing with it."""
    with pytest.raises(Exception):  # noqa: PT011 — pydantic ValidationError
        ChatRequestPayload(session_id="s", text="hi", device_target="wristband")


def test_hup_action_request_payload_accepts_device_target():
    p = HUPActionRequestPayload()
    assert p.device_target is None
    p = HUPActionRequestPayload(name="phone.call.start", device_target="phone")
    assert p.device_target == "phone"


# ───────────────────────── surface registry ──────────────────────────


def test_brain_host_and_phone_actuator_surfaces_registered():
    """The two new surfaces must appear in the registered surfaces tuple
    so `is_tool_allowed` can resolve their deny lists at runtime."""
    surfaces = set(known_surfaces())
    assert "brain_host" in surfaces, (
        "brain_host surface missing — operator's 'do X on my Mac' from "
        "iOS chat depends on this surface existing"
    )
    assert "phone_actuator" in surfaces, (
        "phone_actuator surface missing — Phase 4 iOS skills need this "
        "surface to deny Mac-only tools"
    )


def test_brain_host_allows_mac_actuation_tools():
    """The whole point of `device_target=brain`: iOS chat can ask the
    Mac to do Mac things. `desktop_control__shell_command` /
    `computer_use__bash` / `agentic_computer_use__execute_task` MUST
    be allowed on `brain_host`. If this regresses, the operator is
    back to 'no access' on every Mac action."""
    for tool in (
        "desktop_control__shell_command",
        "desktop_control__open_app",
        "computer_use__bash",
        "agentic_computer_use__execute_task",
        "browser__navigate",
    ):
        assert is_tool_allowed(tool, "brain_host"), (
            f"{tool!r} must be allowed on brain_host for iOS chat to "
            f"actuate the Mac"
        )


def test_brain_host_still_denies_truly_destructive():
    """Even on the trusted brain_host surface, the truly destructive
    primitives stay denied. Defense in depth."""
    for tool in (
        "system.run",
        "docker.exec",
        "shell.exec",
        "fs.delete",
        "file.delete",
    ):
        assert not is_tool_allowed(tool, "brain_host"), (
            f"{tool!r} must remain denied on brain_host"
        )


def test_phone_actuator_denies_mac_tools():
    """When the LLM picks Mac-only tools but `device_target == "phone"`,
    surface deny refuses. This steers the LLM toward the correct
    `phone.*` action vocabulary (Phase 4)."""
    for tool in (
        "desktop_control__shell_command",
        "desktop_control__open_app",
        "computer_use__bash",
        "agentic_computer_use__execute_task",
        "gui_computer_use__screenshot",
    ):
        assert not is_tool_allowed(tool, "phone_actuator"), (
            f"{tool!r} must be denied on phone_actuator — it's a Mac-only "
            f"tool with no meaning on the phone surface"
        )


# ───────────────────────── resolver behavior ──────────────────────────


def test_device_target_brain_overrides_phone_surface_source():
    """The headline operator fix: phone source + device_target=brain
    must resolve to brain_host, NOT the legacy http_api hard-deny."""
    surface = resolve_surface_from_context({
        "source": "phone_surface",
        "device_target": "brain",
    })
    assert surface == "brain_host", (
        "phone source + device_target=brain must resolve to brain_host; "
        "got " + surface
    )


def test_device_target_phone_resolves_to_phone_actuator():
    surface = resolve_surface_from_context({
        "source": "phone_surface",
        "device_target": "phone",
    })
    assert surface == "phone_actuator"


def test_device_target_glasses_resolves_to_phone_actuator():
    """Glasses are bridged through the phone (Phase 9 docs the
    `relay.glasses.*` workflow). Same security envelope as phone."""
    surface = resolve_surface_from_context({
        "source": "phone_surface",
        "device_target": "glasses",
    })
    assert surface == "phone_actuator"


def test_device_target_auto_falls_back_to_source_mapping():
    """`auto` means 'brain decides' — until the PromptRefiner (Phase 2)
    is wired, fall through to the conservative source→surface table
    so behavior matches the pre-fix default."""
    surface = resolve_surface_from_context({
        "source": "phone_surface",
        "device_target": "auto",
    })
    assert surface == "http_api"


def test_missing_device_target_preserves_legacy_behavior():
    """Backward compat: clients that don't send `device_target` see
    the historical source→surface mapping. iOS apps on older builds
    must keep working."""
    surface = resolve_surface_from_context({
        "source": "phone_surface",
    })
    assert surface == "http_api"


def test_privileged_surface_claim_is_rejected():
    """SECURITY (Batch 1 trust-boundary fix #6): a client may NOT escalate
    by declaring a privileged ``context["surface"]``.

    This UPDATES the old ``test_explicit_surface_overrides_everything``,
    which pinned the escape-hatch behavior where a raw
    ``surface="local_cli"`` claim won outright. ``local_cli`` is
    unrestricted, so honoring a client claim let a paired remote node
    bypass every deny list. The claim is now ignored and the surface is
    derived from the trusted transport signals (device_target / source).
    """
    surface = resolve_surface_from_context({
        "surface": "local_cli",
        "source": "phone_surface",
        "device_target": "brain",
    })
    # The local_cli claim is discarded; device_target="brain" (a
    # separately-authorized escalation) still resolves to brain_host.
    assert surface == "brain_host"


def test_paired_remote_node_cannot_escalate_to_local_cli():
    """A paired remote node sending ``context.surface="local_cli"`` must
    not land on the unrestricted local_cli surface; it falls through to
    the transport-derived source mapping (phone_surface → http_api)."""
    surface = resolve_surface_from_context({
        "surface": "local_cli",
        "source": "phone_surface",
    })
    assert surface != "local_cli"
    assert surface == "http_api"


def test_unknown_surface_claim_is_rejected():
    """An unknown surface is unrestricted (is_tool_allowed treats it as
    such), so a client claim of one must be ignored."""
    surface = resolve_surface_from_context({
        "surface": "totally_made_up",
        "source": "voice",
    })
    assert surface == "websocket"


def test_brain_host_surface_claim_is_rejected():
    """brain_host is privileged (Mac control) and reachable only via
    device_target="brain", never a raw surface claim."""
    surface = resolve_surface_from_context({
        "surface": "brain_host",
        "source": "phone_surface",
    })
    assert surface == "http_api"


def test_unprivileged_surface_claim_still_honored():
    """A restrictive surface like http_api only tightens policy, so
    trusted internal callers that set it keep working."""
    surface = resolve_surface_from_context({
        "surface": "http_api",
        "source": "voice",
    })
    assert surface == "http_api"


def test_unknown_device_target_falls_through_to_source():
    """Defense in depth: if the wire validation somehow lets an
    unknown value through (e.g. via dict→context path that bypasses
    Pydantic), the resolver still does something safe."""
    surface = resolve_surface_from_context({
        "source": "phone_surface",
        "device_target": "wristband",  # not in _DEVICE_TARGET_TO_SURFACE
    })
    assert surface == "http_api"


def test_resolve_empty_context_returns_default():
    assert resolve_surface_from_context({}) == "websocket"
    assert resolve_surface_from_context(None) == "websocket"
