"""
Self-introspection skill: lets the agent answer meta-questions about its own
surface. Removes the "I don't know which tools I have" class of failures.

Pulls live data from BrainState: channel manager, skill registry, HUP node
registry, mitosis engine, tool genesis engine. Returns compact JSON that the
LLM can render back to the user in prose.
"""
from __future__ import annotations

import logging
import platform
import socket
from typing import Any, Dict

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skill.self_introspection")


@register_skill
class SelfIntrospectionSkill(BaseSkill):
    def __init__(self):
        super().__init__("self_introspection")

    @staticmethod
    def _state():
        try:
            from api.state import state
            return state
        except Exception:
            return None

    async def execute(
        self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]
    ) -> Dict[str, Any]:
        state = self._state()
        if state is None:
            return {"success": False, "status_code": 503, "data": None, "error": "FERAL brain not ready."}

        if endpoint_id == "list_capabilities":
            return self._list_capabilities(state)
        if endpoint_id == "describe_skill":
            return self._describe_skill(state, args)
        if endpoint_id == "active_channels":
            return self._active_channels(state)
        if endpoint_id == "connected_devices":
            return self._connected_devices(state)
        if endpoint_id == "current_session":
            return self._current_session(state)
        if endpoint_id == "list_specialists":
            return self._list_specialists(state)
        if endpoint_id == "list_pending_proposals":
            return self._list_pending_proposals(state)

        return {"success": False, "status_code": 400, "data": None, "error": f"Unknown endpoint {endpoint_id!r}"}

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _skill_registry(state):
        """Return the live SkillRegistry regardless of legacy attribute name.

        Historically callers used ``state.skills``; canonical is
        ``state.skill_registry``. BrainState now exposes both, but we
        still probe defensively so this skill works against older
        snapshots + tests that mock only one of the two.
        """
        return getattr(state, "skill_registry", None) or getattr(state, "skills", None)

    def _list_capabilities(self, state) -> Dict[str, Any]:
        skills_out = []
        registry = self._skill_registry(state)
        skill_map = getattr(registry, "skills", {}) if registry is not None else {}
        try:
            for skill in skill_map.values():
                endpoints = []
                for ep in getattr(skill, "endpoints", []) or []:
                    endpoints.append({
                        "id": getattr(ep, "id", ""),
                        "description": getattr(ep, "description", ""),
                    })
                skills_out.append({
                    "skill_id": getattr(skill, "skill_id", ""),
                    "name": getattr(getattr(skill, "brand", None), "name", ""),
                    "description": getattr(skill, "description", ""),
                    "endpoints": endpoints,
                })
        except Exception as exc:
            logger.warning("list_capabilities: %s", exc)

        data = {
            "skills": skills_out,
            "active_channels": self._active_channels_payload(state),
            "connected_devices": self._connected_devices_payload(state),
            "autonomy_mode": self._autonomy_mode(state),
            "version": self._version(),
        }
        return {"success": True, "status_code": 200, "data": data, "error": None}

    def _describe_skill(self, state, args: Dict[str, Any]) -> Dict[str, Any]:
        target = (args.get("skill_id") or "").strip()
        if not target:
            return {"success": False, "status_code": 400, "data": None, "error": "skill_id required"}
        registry = self._skill_registry(state)
        skill_map = getattr(registry, "skills", {}) if registry is not None else {}
        skill = skill_map.get(target)
        if skill is None:
            return {"success": False, "status_code": 404, "data": None, "error": f"No skill named {target!r}"}
        endpoints = []
        for ep in getattr(skill, "endpoints", []) or []:
            endpoints.append({
                "id": getattr(ep, "id", ""),
                "description": getattr(ep, "description", ""),
                "params": [
                    {"name": getattr(p, "name", ""), "type": getattr(p, "type", ""), "required": getattr(p, "required", False)}
                    for p in getattr(ep, "params", []) or []
                ],
            })
        # Flows are recipes: an ordered list of this skill's own
        # endpoints that accomplishes one task ("to read a page,
        # navigate then get_page_text"). Nine are declared across six
        # shipped manifests, and the only dispatcher that ever looked at
        # them is the cron ``flow_id`` branch in
        # ``SkillRegistry._flow_to_taskflow_steps``, which one of the
        # nine uses. Six were referenced by nothing at all, and nothing
        # showed them to the model either, so a sequence somebody wrote
        # down for combining these endpoints was unreachable from every
        # direction.
        #
        # Reporting them here rather than building a second dispatcher
        # is deliberate: see ``ProactiveEngine._evaluate_manifest_triggers``
        # for what a new automatic execution path in this area cost the
        # last time. The model that would have followed the recipe can
        # now read it and call the endpoints itself, under the same
        # gates every other tool call goes through.
        flows = []
        for flow in getattr(skill, "flows", []) or []:
            flows.append({
                "id": getattr(flow, "id", ""),
                "description": getattr(flow, "description", ""),
                "steps": [
                    getattr(s, "endpoint_id", "")
                    for s in (getattr(flow, "steps", []) or [])
                    if getattr(s, "endpoint_id", "")
                ],
            })
        return {
            "success": True,
            "status_code": 200,
            "data": {
                "skill_id": getattr(skill, "skill_id", ""),
                "name": getattr(getattr(skill, "brand", None), "name", ""),
                "description": getattr(skill, "description", ""),
                "endpoints": endpoints,
                "flows": flows,
                # The hourly ceiling ``SkillExecutor`` enforces, so the
                # caller can see the budget it is spending rather than
                # discovering it as a 429.
                "max_calls_per_hour": getattr(skill, "max_calls_per_hour", 0),
            },
            "error": None,
        }

    def _active_channels(self, state) -> Dict[str, Any]:
        return {"success": True, "status_code": 200, "data": {"channels": self._active_channels_payload(state)}, "error": None}

    def _connected_devices(self, state) -> Dict[str, Any]:
        devices, error = self._connected_devices_result(state)
        data: Dict[str, Any] = {"devices": devices}
        # Disconnected nodes. `state.device_registry` is emptied by the
        # WebSocketDisconnect teardown in api/server.py, so a phone that
        # dropped ten seconds ago was invisible here and the model
        # answered "you have no devices connected" for hardware the user
        # was still wearing. The sub-device store is the only record
        # that outlives the socket, so the offline half comes from
        # there via the same assembler /api/devices/connected uses.
        data.update(self._offline_devices_payload(state, devices))
        if error:
            # An empty list is a legitimate answer meaning "nothing is
            # attached". Returning it on a crash made a broken registry
            # read identically to a bare machine, and the model then
            # told the user they had no devices connected. Carry the
            # reason so the answer can distinguish the two.
            data["error"] = error
        return {"success": True, "status_code": 200, "data": data, "error": None}

    def _current_session(self, state) -> Dict[str, Any]:
        model = "unknown"
        try:
            llm = getattr(state, "llm_client", None) or getattr(state, "llm", None)
            if llm is not None:
                provider = getattr(llm, "provider", None) or type(llm).__name__
                name = getattr(llm, "model_name", None) or getattr(llm, "model", None) or "default"
                model = f"{provider}/{name}"
        except Exception:
            pass
        return {
            "success": True,
            "status_code": 200,
            "data": {
                "version": self._version(),
                "model": model,
                "host": socket.gethostname(),
                "os": platform.system(),
                "autonomy_mode": self._autonomy_mode(state),
            },
            "error": None,
        }

    def _list_specialists(self, state) -> Dict[str, Any]:
        specialists = []
        try:
            engine = getattr(state, "agent_mitosis", None) or getattr(state, "mitosis_engine", None)
            if engine and hasattr(engine, "list_specialists"):
                for sp in engine.list_specialists():
                    specialists.append({
                        "id": getattr(sp, "id", None) or sp.get("id"),
                        "domain": getattr(sp, "domain", None) or sp.get("domain"),
                        "allowed_skills": list(getattr(sp, "allowed_skills", None) or sp.get("allowed_skills") or []),
                        "confidence": getattr(sp, "confidence", None) or sp.get("confidence", 0.0),
                    })
        except Exception as exc:
            logger.debug("list_specialists: %s", exc)
        return {"success": True, "status_code": 200, "data": {"specialists": specialists}, "error": None}

    def _list_pending_proposals(self, state) -> Dict[str, Any]:
        proposals = []
        try:
            engine = getattr(state, "tool_genesis", None) or getattr(state, "tool_genesis_engine", None)
            if engine and hasattr(engine, "list_pending_proposals"):
                for prop in engine.list_pending_proposals():
                    proposals.append(prop if isinstance(prop, dict) else {
                        "tool_id": getattr(prop, "tool_id", ""),
                        "name": getattr(prop, "name", ""),
                        "pattern": getattr(prop, "pattern", ""),
                        "autonomy": getattr(prop, "autonomy", ""),
                        "created_at": getattr(prop, "created_at", None),
                        "preview": getattr(prop, "preview", ""),
                    })
        except Exception as exc:
            logger.debug("list_pending_proposals: %s", exc)
        return {"success": True, "status_code": 200, "data": {"proposals": proposals}, "error": None}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _active_channels_payload(state) -> list[dict]:
        out: list[dict] = []
        cm = getattr(state, "channel_manager", None)
        if not cm:
            return out
        for ctype, ch in cm.channels.items():
            out.append({
                "name": ctype,
                "running": bool(getattr(ch, "_running", False)),
                "bot_username": getattr(ch, "_bot_username", None),
            })
        return out

    @staticmethod
    def _connected_devices_payload(state) -> list[dict]:
        """Back-compat wrapper: devices only, error dropped."""
        devices, _error = SelfIntrospectionSkill._connected_devices_result(state)
        return devices

    @staticmethod
    def _offline_devices_payload(state, live_devices: list[dict]) -> dict:
        """Nodes the brain knows about that are NOT reporting right now.

        Returns ``{"offline": [...], "heartbeat_window_s": N}``, or an
        empty dict when there is no sub-device store to read (older
        snapshots and every test double that predates it), so this can
        never fabricate an offline device out of nothing.
        """
        store = getattr(state, "node_subdevices", None)
        if store is None:
            return {}
        try:
            rows = store.list_all()
        except Exception as exc:
            logger.warning(
                "node_subdevices.list_all failed; the tool cannot report "
                "disconnected devices this turn: %s", exc, exc_info=True,
            )
            return {"offline_error": f"sub-device store unavailable: {exc}"}
        from api.device_view import build_device_view

        view = build_device_view(
            live_nodes=[
                {"node_id": d.get("node_id") or "", "type": d.get("type"),
                 "name": d.get("name")}
                for d in live_devices
                if d.get("node_id")
            ],
            subdevice_rows=rows,
        )
        return {
            "offline": view["offline"],
            "heartbeat_window_s": view["heartbeat_window_s"],
        }

    @staticmethod
    def _subdevices_payload(state, node_id: str) -> list[dict]:
        """Peripherals paired BEHIND ``node_id``, from the truth store.

        ``device_registry`` knows about HUP nodes -- the iPhone. It has
        never known about what is paired to the iPhone. The BLE
        peripherals (W300 glasses, VITRO wristband) live only in
        ``state.node_subdevices``, which ``/api/devices/connected``
        already merges in via ``_subdevices_for()``. Without this the
        dashboard and this tool answered "what is connected" with
        different facts, and the tool -- the one the model reads -- was
        the one missing the hardware.

        ``live`` is carried through deliberately: "paired" and
        "currently reporting" are different claims and the model must
        be able to tell the user which one it has.
        """
        store = getattr(state, "node_subdevices", None)
        if store is None or not node_id:
            return []
        try:
            rows = store.list_for_node(node_id)
        except Exception as exc:
            logger.warning(
                "node_subdevices.list_for_node(%s) failed; reporting the node "
                "with no peripherals: %s", node_id, exc, exc_info=True,
            )
            return []
        out = []
        for row in rows or []:
            attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
            out.append({
                "capability": row.get("capability"),
                "name": attrs.get("device_name") or "",
                "status": row.get("status"),
                "live": bool(row.get("live")),
                "provenance": row.get("provenance"),
                "last_seen": row.get("last_seen"),
                "attrs": attrs,
            })
        return out

    @staticmethod
    def _connected_devices_result(state) -> tuple[list[dict], str]:
        """Return ``(devices, error_text)`` for devices known to
        ``state.device_registry``, each with its sub-device tree.

        Falls back to legacy ``node_registry`` / ``nodes`` attributes for
        any caller that still exposes them. The canonical path uses
        ``DeviceRegistry.list_devices()`` (see ``hardware/protocol.py``);
        anything else is best-effort.

        The old body ended in ``except Exception: return []``. Because
        ``[]`` is also the honest answer for a machine with nothing
        attached, a registry that raised produced a confident "no
        devices are connected" with no trace anywhere. The error text is
        now returned to the caller and logged.
        """
        try:
            registry = (
                getattr(state, "device_registry", None)
                or getattr(state, "node_registry", None)
                or getattr(state, "nodes", None)
            )
            if not registry:
                return [], ""
            if hasattr(registry, "list_devices"):
                raw = registry.list_devices()
            elif hasattr(registry, "list_nodes"):
                raw = registry.list_nodes()
            elif hasattr(registry, "all"):
                raw = registry.all()
            elif hasattr(registry, "nodes"):
                raw = list(registry.nodes.values())
            elif hasattr(registry, "_devices"):
                raw = list(registry._devices.values())
            else:
                return [], (
                    f"device registry {type(registry).__name__!r} exposes no "
                    "known listing method (list_devices / list_nodes / all / "
                    "nodes / _devices); devices cannot be enumerated"
                )
            devices = []
            for node in raw:
                if isinstance(node, dict):
                    caps = node.get("capabilities") or []
                    devices.append({
                        "node_id": node.get("device_id") or node.get("node_id") or node.get("id"),
                        "type": node.get("device_type") or node.get("type"),
                        "name": node.get("name"),
                        "capabilities": [getattr(c, "category", c) if not isinstance(c, str) else c for c in caps],
                        "last_seen": node.get("last_seen"),
                    })
                    devices[-1]["subdevices"] = SelfIntrospectionSkill._subdevices_payload(
                        state, devices[-1]["node_id"] or "",
                    )
                else:
                    caps_raw = list(getattr(node, "capabilities", []) or [])
                    caps = [getattr(c, "category", None) or getattr(c, "name", None) or str(c) for c in caps_raw]
                    devices.append({
                        "node_id": getattr(node, "device_id", None) or getattr(node, "node_id", None) or getattr(node, "id", None),
                        "type": getattr(node, "device_type", None) or getattr(node, "type", None),
                        "name": getattr(node, "name", None),
                        "capabilities": caps,
                        "last_seen": getattr(node, "last_seen", None),
                    })
                    devices[-1]["subdevices"] = SelfIntrospectionSkill._subdevices_payload(
                        state, devices[-1]["node_id"] or "",
                    )
            return devices, ""
        except Exception as exc:
            logger.warning(
                "connected_devices enumeration failed: %s", exc, exc_info=True,
            )
            return [], f"device registry unavailable: {exc}"

    @staticmethod
    def _autonomy_mode(state) -> str:
        try:
            cfg = getattr(state, "config", None)
            if cfg and hasattr(cfg, "get_setting"):
                return str(cfg.get_setting("autonomy_mode") or "hybrid")
        except Exception:
            pass
        return "hybrid"

    @staticmethod
    def _version() -> str:
        try:
            import importlib.metadata as md
            return md.version("feral-ai")
        except Exception:
            return "dev"
