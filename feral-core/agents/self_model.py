"""
Unified self-model shared by the chat identity loader and the voice identity
workspace. Centralizes:

- Live runtime line (version, host, os, model, channels, devices, autonomy,
  capabilities) — the environment preamble every turn gets.
- Prose `## Tooling` catalog — enumerates every registered skill and endpoint
  so the LLM can see both the full catalog AND which skills are active for
  this turn. Removes the "I don't have a skill for that" failure mode.
- UI route map — gives the agent a stable map of what pages exist in the web
  UI so it can reference them when steering the user.

This is intentionally a single function that builds plain text. Both chat
(`agents/identity_loader.py`) and voice (`identity/workspace.py`) call it so
we cannot drift between the two surfaces.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from models.skill_manifest import SkillManifest

logger = logging.getLogger("feral.self_model")


# ---------------------------------------------------------------------------
# UI routes. Keep in sync with feral-client-v2/src/App.jsx (this used to
# name feral-client/src/main.jsx, the v1 router, which was deleted in
# 2026.8.12). NOTE: the list below has drifted from the v2 router and is
# left as-is here deliberately, because changing what the brain tells the
# model about its own surfaces is a behaviour change, not a rename.
# Known drift as of this edit: "/ambient" is a redirect to "/" in v2, and
# "/taskflows" does not exist there (the route is "/flows").
# ---------------------------------------------------------------------------

UI_ROUTES: list[tuple[str, str]] = [
    ("/chat", "Main conversational surface. Primary place the user talks to you."),
    ("/settings", "LLM keys, channels, voice, autonomy mode, proposed skills, marketplace."),
    ("/glass-brain", "Real-time neural visualization — tool calls, events, memory writes."),
    ("/timeline", "Chronological episode log of past conversations + actions."),
    ("/ambient", "Passive awareness dashboard (sensors, environment, somatic)."),
    ("/taskflows", "Saved multi-step workflows + scheduling."),
    ("/intents", "Intents catalog — recurring user goals learned over time."),
]


# ---------------------------------------------------------------------------
# Runtime line + capabilities detection
# ---------------------------------------------------------------------------

def _detect_capabilities() -> list[str]:
    """Return the static + dynamic capability tags we expose to the model."""
    caps = ["voice", "vision", "somatic", "mdns", "sdui", "tool_genesis", "mitosis"]
    return caps


def _current_llm_model() -> str:
    try:
        from api.state import state as _state
        llm = getattr(_state, "llm_client", None) or getattr(_state, "llm", None)
        if llm is None:
            return "unknown"
        provider = getattr(llm, "provider", None) or type(llm).__name__
        model = getattr(llm, "model_name", None) or getattr(llm, "model", None) or "default"
        return f"{provider}/{model}"
    except Exception:
        return "unknown"


def _active_channels() -> list[str]:
    try:
        from api.state import state as _state
        cm = getattr(_state, "channel_manager", None)
        if not cm:
            return []
        rows: list[str] = []
        for ctype, ch in cm.channels.items():
            bot = getattr(ch, "_bot_username", None)
            running = bool(getattr(ch, "_running", False) or getattr(ch, "_connected", False))
            if not running:
                continue
            rows.append(f"{ctype}(@{bot})" if bot else ctype)
        return rows
    except Exception:
        return []


def _connected_devices(frame) -> list[str]:
    try:
        nodes = getattr(frame, "connected_nodes", None) or []
        if isinstance(nodes, str):
            return [nodes]
        return list(nodes)
    except Exception:
        return []


def _autonomy_mode() -> str:
    # v2026.5.26: prefer the live ToolRunner state (single source of
    # truth) rather than reading settings.json — the runtime value
    # reflects WebUI mid-session changes that the persisted file
    # captures only after a future POST. Falls back to the persisted
    # value, then "hybrid".
    try:
        from api.state import state as _state
        orch = getattr(_state, "orchestrator", None)
        if orch and hasattr(orch, "tool_runner"):
            live = getattr(orch.tool_runner, "autonomy_mode", "")
            if live:
                return str(live)
        cfg = getattr(_state, "config", None)
        if cfg:
            # ConfigLoader exposes ``get(section, key)``, NOT
            # ``get_setting`` — the legacy hasattr check silently
            # always missed.
            getter = getattr(cfg, "get_setting", None) or getattr(cfg, "get", None)
            if getter:
                try:
                    val = getter("security", "autonomy_mode")
                except TypeError:
                    val = getter("autonomy_mode")
                if val:
                    return str(val)
    except Exception:
        pass
    return "hybrid"


def _feral_version() -> str:
    try:
        import importlib.metadata as md
        return md.version("feral-ai")
    except Exception:
        try:
            from version import __version__ as v  # type: ignore
            return str(v)
        except Exception:
            return "dev"


def build_runtime_line(frame=None) -> str:
    """One-line environment summary appended near the bottom of the system prompt.

    Example:
        Runtime: agent=feral version=2026.9.2 host=mbp os=Darwin model=openai/gpt-4o channels=telegram(@feral_bot) devices=wristband autonomy=hybrid capabilities=voice,vision,somatic,mdns,sdui,tool_genesis,mitosis
    """
    version = _feral_version()
    host = socket.gethostname() or "localhost"
    os_name = platform.system() or "unknown"
    model = _current_llm_model()
    channels = _active_channels()
    channels_s = ",".join(channels) if channels else "none"
    devices = _connected_devices(frame) if frame is not None else []
    devices_s = ",".join(str(d) for d in devices) if devices else "none"
    autonomy = _autonomy_mode()
    caps = ",".join(_detect_capabilities())
    return (
        f"Runtime: agent=feral version={version} host={host} os={os_name} "
        f"model={model} channels={channels_s} devices={devices_s} "
        f"autonomy={autonomy} capabilities={caps}"
    )


# ---------------------------------------------------------------------------
# Prose tooling catalog
# ---------------------------------------------------------------------------

def _skill_line(skill, prefix: str = "") -> str:
    """Render ONE skill + its endpoints as a prose bullet block."""
    skill_id = getattr(skill, "skill_id", None) or getattr(getattr(skill, "brand", None), "name", "")
    name = getattr(getattr(skill, "brand", None), "name", skill_id) or skill_id
    description = getattr(skill, "description", "") or ""
    description = (description.splitlines()[0] if description else "").strip()
    header = f"{prefix}- **{name}** (`{skill_id}`) — {description or 'FERAL skill.'}"
    endpoints = getattr(skill, "endpoints", []) or []
    ep_lines: list[str] = []
    for ep in endpoints[:8]:  # cap per-skill to keep prompt budget sane
        ep_id = getattr(ep, "id", "")
        ep_desc = getattr(ep, "description", "") or ""
        ep_desc = (ep_desc.splitlines()[0] if ep_desc else "").strip()
        if not ep_id:
            continue
        ep_lines.append(f"{prefix}  - `{skill_id}__{ep_id}`: {ep_desc}")
    if len(endpoints) > 8:
        ep_lines.append(f"{prefix}  - …and {len(endpoints) - 8} more endpoints.")
    return "\n".join([header, *ep_lines])


def _inactive_skill_line(skill) -> str:
    """Render discovery metadata without advertising unavailable endpoints.

    Inactive skills are registered on the Brain but do not have callable tool
    schemas on the current turn.  Keeping their identity and description lets
    the model acknowledge the installed capability, while omitting endpoint
    prose avoids spending context on instructions it cannot act on yet.
    """
    skill_id = getattr(skill, "skill_id", None) or getattr(
        getattr(skill, "brand", None), "name", ""
    )
    name = getattr(getattr(skill, "brand", None), "name", skill_id) or skill_id
    description = getattr(skill, "description", "") or ""
    description = (description.splitlines()[0] if description else "").strip()
    return (
        f"- **{name}** (`{skill_id}`) — {description or 'FERAL skill.'} "
        "Registered, but not callable this turn."
    )


def build_tooling_catalog(
    active: Iterable,
    full: Iterable,
    max_full: int = 80,
) -> str:
    """Build the prose `## Tooling` block.

    The section has two sub-lists:

    - **Active this turn** — the routed skills (plus always-include) that the
      model has real tool definitions for on this call. These are the ones it
      can actually invoke.
    - **Available (full catalog)** — every registered skill. This PROVES to
      the model that a capability exists so it cannot refuse with "I don't
      have a tool for that"; if a skill is in the catalog but not active, the
      model can ask the user to re-route or explicitly request routing.
    """
    active_list = list(active or [])
    full_list = list(full or [])
    active_ids = {getattr(s, "skill_id", None) for s in active_list}

    parts: list[str] = ["## Tooling"]
    if active_list:
        parts.append("### Active this turn")
        parts.append(
            "These skills are loaded as tools right now. You can call them "
            "directly via `skill_id__endpoint_id` as usual."
        )
        for s in active_list:
            parts.append(_skill_line(s))
    else:
        parts.append("### Active this turn\n(none routed — rely on the always-include fallback set)")

    if full_list:
        parts.append("\n### Available (full catalog)")
        parts.append(
            "Every skill registered on this FERAL instance. If a capability "
            "here isn't active right now, say so explicitly — never claim "
            "the skill does not exist."
        )
        dimmed = []
        seen_ids = set(active_ids)
        for skill in full_list:
            skill_id = getattr(skill, "skill_id", None)
            if skill_id in seen_ids:
                continue
            seen_ids.add(skill_id)
            dimmed.append(skill)
        for s in dimmed[:max_full]:
            parts.append(_inactive_skill_line(s))
        if len(dimmed) > max_full:
            parts.append(f"- …and {len(dimmed) - max_full} more skills. Ask the user to be specific.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# UI route section
# ---------------------------------------------------------------------------

def build_ui_route_map() -> str:
    lines = ["## UI Pages",
             "FERAL has a web UI. When pointing the user somewhere, use these routes:"]
    for path, desc in UI_ROUTES:
        lines.append(f"- `{path}` — {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public: build_core_self_model
# ---------------------------------------------------------------------------

def build_core_self_model(
    *,
    frame=None,
    active_skills: Optional[Iterable] = None,
    full_skills: Optional[Iterable] = None,
    include_ui_routes: bool = True,
) -> str:
    """Core self-model block — shared by chat + voice identity builders.

    Returns a single text block ready to append to the system prompt. Safe to
    call even when state is partially initialized (missing pieces are just
    omitted).
    """
    sections: list[str] = []

    if active_skills is not None or full_skills is not None:
        sections.append(
            build_tooling_catalog(active_skills or [], full_skills or [])
        )

    if include_ui_routes:
        sections.append(build_ui_route_map())

    sections.append(build_runtime_line(frame))

    return "\n\n".join(s for s in sections if s.strip())
