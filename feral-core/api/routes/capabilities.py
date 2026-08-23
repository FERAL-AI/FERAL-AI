"""Phase 5 (audit-r10 overhaul) — capability discovery REST surface.

Exposes the union of brain-host skill manifests (from
`state.skill_registry`) and currently-connected node skill manifests
(from `state.capability_registry`, populated by `node_register`
frames per Phase 4).

Two readers:

* Web + iOS clients render a "what can this brain do right now" pane
  so the user can see exactly which `phone.*` / `glasses.*` actions
  are live as devices connect / disconnect — no more silent failures
  when the operator's iPhone is asleep.

* The orchestrator's capability-aware routing (also Phase 5)
  consults this in-process via `capability_registry.find_handler(...)`;
  the REST surface is for human-facing UIs.

Wire shape::

    GET /api/capabilities
    {
      "brain_host": [
        { "id", "name", "description", "category", ... }  # SkillManifest
      ],
      "nodes": [
        {
          "node_id", "node_type", "platform", "surface",
          "skills": [
            { "id", "name", "description",
              "actions": [
                { "name", "summary", "requiresPermission" }
              ]
            }
          ]
        }
      ],
      "primary_session_id": "<uuid>"
    }
"""
from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass

from fastapi import APIRouter, HTTPException

from api.state import state

logger = logging.getLogger("feral.api.capabilities")

router = APIRouter(tags=["capabilities"])


def _serialize_brain_host_skill(skill_id: str, manifest: object) -> dict:
    """Return a JSON-safe shape for a SkillManifest.

    The manifest type lives in `skills/registry.py` and is a small
    dataclass-ish object today; this helper stays defensive in case
    it grows or someone hands us a dict directly.
    """
    if isinstance(manifest, dict):
        out = dict(manifest)
        out.setdefault("id", skill_id)
        return out
    if is_dataclass(manifest):
        out = asdict(manifest)
        out.setdefault("id", skill_id)
        return out
    out: dict = {"id": skill_id}
    for attr in (
        "name", "description", "category",
        "endpoints", "version", "policy_class",
    ):
        if hasattr(manifest, attr):
            try:
                out[attr] = getattr(manifest, attr)
            except Exception:
                pass
    return out


@router.get("/api/capabilities")
async def get_capabilities():
    """Return the live brain-host + connected-node capability catalog.

    Read-only. Safe for unauthenticated polling on the local network.
    Phase 6 wraps individual capability entries with permission cards
    when an action returns `permission_denied:<NSKey>` at runtime;
    the static manifest here doesn't carry permission state.

    The brain-host section is the union of:
      * Phase 11 ``desktop_control`` skill manifests (FaceTime / Music /
        Messages / Notes / URL / app / notify) tracked by the
        capability registry — these carry the same shape as node
        skills (id / name / description / actions[]) so the iOS
        BrainNetworkSection renders them identically.
      * Legacy ``SkillRegistry`` entries (web automation, Phase 5+
        connectors, etc.) shaped via _serialize_brain_host_skill.
    """
    brain_host_skills: list[dict] = []

    # Phase 11 — structured desktop_control manifests.
    try:
        for skill in state.capability_registry.brain_host_skills():
            brain_host_skills.append(dict(skill))
    except Exception as exc:
        logger.warning("brain_host capability_registry read failed: %s", exc)

    # Legacy SkillRegistry entries (different shape; keep for back-compat).
    skill_registry = getattr(state, "skill_registry", None)
    if skill_registry is not None:
        try:
            for sid, manifest in getattr(skill_registry, "skills", {}).items():
                brain_host_skills.append(_serialize_brain_host_skill(sid, manifest))
        except Exception as exc:
            logger.warning("brain_host skill enumeration failed: %s", exc)

    nodes = state.capability_registry.snapshot_nodes()

    return {
        "brain_host": brain_host_skills,
        "nodes": nodes,
        "primary_session_id": state.primary_session_id,
        "connected_node_count": len(nodes),
    }


@router.get("/api/capabilities/has")
async def has_capability(action: str | None = None, node_type: str | None = None):
    """Cheap routability probe for clients / the orchestrator.

    Either pass `action=<name>` (e.g. `phone.call.start`) to ask
    "is this action handleable right now?", or `node_type=<kind>`
    (e.g. `phone`) to ask "do I have one of those connected?".
    Returns `{ "available": bool, "handler": {...}? }`.
    """
    if action:
        handler = state.capability_registry.find_handler(action)
        if handler is None:
            return {"available": False, "action": action}
        return {
            "available": True,
            "action": action,
            "handler": {
                "surface": handler.surface,
                "node_id": handler.node_id,
                "node_type": handler.node_type,
                "platform": handler.platform,
            },
        }
    if node_type:
        return {
            "available": state.capability_registry.has_node_type(node_type),
            "node_type": node_type,
        }
    # 400, not a 200 saying `available: false`.
    #
    # A caller that forgot both parameters was told the capability is
    # unavailable, which is a confident answer to a question nobody
    # asked. A client reading `available` cannot tell "you have no phone
    # connected" from "you called this endpoint wrong", and would
    # reasonably conclude the hardware is missing. Unknown must not be
    # representable as a plausible answer.
    raise HTTPException(
        status_code=400,
        detail="pass either `action` or `node_type`",
    )
