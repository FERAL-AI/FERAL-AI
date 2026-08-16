"""Security vault/permissions/audit, HUP hardware API, sandbox policy, hardware mesh REST."""

import re

from fastapi import APIRouter, HTTPException

from api.state import state
from hardware.protocol import HUPAction, HUPActionType
from security.safe_regex import UnsafePatternError, compile_safe_regex
from security.sandbox_policy import SandboxPolicy
from security.vault import PermissionTier

router = APIRouter()


# ─────────────────────────────────────────────
# Security API
# ─────────────────────────────────────────────


@router.get("/api/security/vault")
async def vault_summary():
    """Key names + fingerprints — never raw values."""
    if not state.vault:
        return {"keys": {}}
    return {"keys": state.vault.to_safe_summary()}


@router.post("/api/security/vault/store")
async def vault_store(body: dict):
    """Store a credential in the blind vault."""
    name = body.get("key_name", "")
    value = body.get("value", "")
    if not name or not value:
        return {"error": "key_name and value are required"}
    state.vault.store(name, value, stored_by="api")
    return {"ok": True, "key_name": name, "fingerprint": state.vault.fingerprint(name)}


@router.delete("/api/security/vault/{key_name}")
async def vault_remove(key_name: str):
    removed = state.vault.remove(key_name, removed_by="api")
    return {"ok": removed}


@router.get("/api/security/permissions")
async def get_permissions():
    """Current permission tier and sandbox status."""
    return {
        "max_tier": state.sandbox.max_tier if state.sandbox else "active",
        "tiers": PermissionTier.TIER_ORDER,
        "tier_descriptions": {
            "passive": "Read-only, no side effects (weather, search)",
            "active": "Can send data (messaging, calendar)",
            "privileged": "Can modify system state (file access)",
            "dangerous": "Destructive operations (delete, financial)",
        },
    }


@router.post("/api/security/permissions/update")
async def update_permissions(body: dict):
    new_tier = body.get("max_tier", "active")
    if new_tier not in PermissionTier.TIER_ORDER:
        return {"error": f"Invalid tier: {new_tier}"}
    if state.sandbox:
        state.sandbox.max_tier = new_tier
    return {"ok": True, "max_tier": new_tier}


@router.get("/api/security/audit")
async def get_audit_log():
    """Get recent security audit entries.

    audit-r12 A5 (v2026.5.38) — delegates to
    :func:`security.audit_log.recent_events` so the dashboard
    surfaces "audit log unreadable" (HTTP 500 with a structured
    error) instead of pretending the log is empty when the file is
    present but unreadable / malformed.
    """
    from security.audit_log import recent_events, AuditFailure

    try:
        entries = recent_events(limit=100)
    except AuditFailure as exc:
        return {"error": "audit_log_unreadable", "detail": str(exc), "entries": []}
    return {"entries": entries}


# ─────────────────────────────────────────────
# Hardware Use Protocol (HUP) API
# ─────────────────────────────────────────────


@router.get("/api/hardware/devices")
async def list_hardware_devices():
    """List all registered hardware devices."""
    if not state.device_registry:
        return {"devices": []}
    devices = state.device_registry.list_devices()
    return {"devices": [d.model_dump() for d in devices]}


@router.get("/api/hardware/device/{device_id}")
async def get_hardware_device(device_id: str):
    if not state.device_registry:
        return {"error": "No device registry"}
    device = state.device_registry.get_device(device_id)
    if not device:
        return {"error": f"Device not found: {device_id}"}
    return device.model_dump()


# Capability ids that mean "take a picture". ``hardware/protocol.py``'s
# reference glasses manifest calls it ``capture_photo`` and
# ``hardware/mesh.py``'s phone node calls it ``camera_snap``; both declare
# ``category="sensor"`` but neither uses the ``read_`` prefix, so the
# sensor allowlist below never saw them. The suffix/substring forms cover
# self-describing third-party devices that pick their own spelling.
_CAMERA_CAPTURE_IDS: frozenset = frozenset({
    "capture_photo", "camera_snap", "capture_image", "take_photo", "snapshot",
})
_CAMERA_CAPTURE_TOKENS: tuple = ("camera", "photo")

# Capability categories that drive something physical. ``sensor`` is read
# only and handled by the sensor allowlist; ``network`` / ``compute`` are
# not actuation.
_ACTUATOR_CATEGORIES: frozenset = frozenset({"actuator", "display", "audio"})

# Capability ids that stop hardware. See ``SandboxPolicy.emergency_stop_enabled``.
_EMERGENCY_STOP_IDS: frozenset = frozenset({"halt", "estop", "emergency_stop", "stop"})


def _is_camera_capture(capability_id: str) -> bool:
    cap = (capability_id or "").lower()
    if cap in _CAMERA_CAPTURE_IDS:
        return True
    if cap.startswith("camera_") or cap.endswith("_camera"):
        return True
    return "capture" in cap and any(token in cap for token in _CAMERA_CAPTURE_TOKENS)


def _policy_refusal(capability_id: str, reason: str) -> dict:
    """One refusal shape, naming the check and the capability.

    "Blocked by sandbox policy" with no further detail was the pre-existing
    message and gave the operator nothing to act on: the fix is always a
    named list in ``~/.feral/policies/default.yaml``, so say which one.
    """
    return {"error": f"Blocked by sandbox policy: {reason} ('{capability_id}')."}


def _policy_verdict(action: HUPAction) -> dict | None:
    """Consult ``state.policy`` for one hardware action. None means allowed.

    audit P0.4. Before this, ``can_read_sensor`` here was the ONLY
    production reader of ``state.policy`` anywhere in the brain, so
    ``hardware.actuators`` and ``hardware.cameras`` were configuration that
    did nothing. The capability's ``category`` comes from the device's own
    self-description, which is what decides which allowlist applies.
    """
    policy = state.policy
    if policy is None:
        return None
    capability_id = action.capability_id or ""

    # Preserved verbatim from the original call site: the ``read_<sensor>``
    # naming convention is what the shipped sensor allowlist is written
    # against, and it works without resolving the device manifest.
    if capability_id.startswith("read_"):
        if not policy.can_read_sensor(capability_id.replace("read_", "")):
            return _policy_refusal(capability_id, "sensor is not in hardware.sensors.allowed")
        return None

    if _is_camera_capture(capability_id):
        if not policy.can_capture_camera():
            return _policy_refusal(capability_id, "hardware.cameras.allowed is false")
        return None

    # Everything else needs the device manifest to know whether it actuates.
    device = state.device_registry.get_device(action.device_id) if state.device_registry else None
    capability = None
    if device is not None:
        capability = next(
            (c for c in device.capabilities if c.id == capability_id), None,
        )
    if capability is None:
        # Unknown device or unknown capability. Not a policy question:
        # ``DeviceRegistry.execute_action`` refuses both with a message
        # that says which, and inventing a policy refusal here would hide
        # the real reason.
        return None

    category = (capability.category or "").lower()
    if category == "sensor":
        if not policy.can_read_sensor(capability_id):
            return _policy_refusal(capability_id, "sensor is not in hardware.sensors.allowed")
        return None

    if category in _ACTUATOR_CATEGORIES:
        if capability_id.lower() in _EMERGENCY_STOP_IDS and policy.emergency_stop_enabled():
            return None
        allowed, _needs_confirm = policy.can_use_actuator(capability_id)
        if not allowed:
            return _policy_refusal(
                capability_id, "actuator is not in hardware.actuators.allowed",
            )
    return None


@router.post("/api/hardware/execute")
async def execute_hardware_action(body: dict):
    """Execute a HUP action on a device."""
    if not state.device_registry:
        return {"error": "No device registry"}
    action = HUPAction(
        device_id=body.get("device_id", ""),
        capability_id=body.get("capability_id", ""),
        action_type=HUPActionType(body.get("action_type", "execute")),
        parameters=body.get("parameters", {}),
        timeout_ms=body.get("timeout_ms", 5000),
        confirmed=bool(body.get("confirmed", False)),
    )

    refusal = _policy_verdict(action)
    if refusal is not None:
        return refusal

    result = await state.device_registry.execute_action(action)
    return result.model_dump()


@router.get("/api/hardware/context")
async def hardware_llm_context():
    """Get hardware context string for LLM."""
    if not state.device_registry:
        return {"context": "No hardware devices connected."}
    return {"context": state.device_registry.to_llm_context()}


@router.get("/api/hardware/stats")
async def hardware_stats():
    if not state.device_registry:
        return {}
    return state.device_registry.stats


# ─────────────────────────────────────────────
# Sandbox Policy API
# ─────────────────────────────────────────────
#
# ``POST /api/policy/update`` used to be three lines with no validation:
#
#     state.policy = SandboxPolicy(body); state.policy.save(); return {"ok": True}
#
# Any JSON object replaced the live sandbox policy and the caller was told
# it worked. That is worse than it sounds, because ``SandboxPolicy`` reads
# every field through ``.get(default)``: a typo does not raise, it silently
# selects a default. Until audit P0.4, four of those defaults were allow-all
# (``can_read_sensor`` / ``can_use_actuator`` / ``can_use_mcp_server`` all
# returned True against an empty list, and ``can_capture_camera`` returned
# True against an absent ``cameras`` section), so a typo'd section key was a
# silent widening. Those four now DENY on an empty or absent allowlist,
# matching ``can_access_domain`` in the same class — a typo'd key is now a
# refusal the operator sees rather than a hole they do not. It is still
# worth catching here, because a refusal is also a broken deployment.
# ``applescript.denied_phrases`` set to a non-list returns ``[]`` from
# ``applescript_denied_phrases()``, which re-enables ``do shell script``.
# ``execution.allow_shell_commands: "false"`` is a truthy string, so the
# shell gate reads as ON.
#
# Note for anyone hand-editing a policy file: ``mcp.allowed_servers`` takes
# ``["*"]`` (the shipped default) to mean "any server". ``[]`` is an
# allowlist with nothing on it and denies every MCP connection.
#
# So the validator below checks the types and value domains that
# ``SandboxPolicy``'s own accessors depend on, and nothing else. It does
# NOT require sections to be present: ``tests/test_sandbox_policy.py::
# test_custom_policy`` builds a valid policy that omits ``filesystem``,
# ``memory``, ``daemon`` and ``wasm``, and inventing a required-section
# schema here would contradict the class. It DOES reject keys the class
# has no accessor for, because an ignored key is exactly how a typo
# ("allowd_domains", "netwrok") turns into a silently-defaulted check.
#
# The spec lives here rather than in ``security/sandbox_policy.py`` only
# because this route is the single writer of operator-supplied policy
# documents; the class is also constructed from in-repo dicts and from
# files an operator hand-edits, neither of which goes through HTTP.

_TIERS: tuple[str, ...] = tuple(PermissionTier.TIER_ORDER)

# ``can_access_domain`` enforces ``allowed_domains`` only when mode is
# exactly "allowlist"; ANY other string silently means allow-everything.
# "denylist" is the second mode the suite already exercises
# (tests/test_sandbox_policy.py::test_custom_policy).
_NETWORK_MODES: tuple[str, ...] = ("allowlist", "denylist")

# Field spec grammar. Each leaf is (kind, *args); each dict is a nested
# section that must itself be a JSON object.
#
#   ("str",)                 non-empty string
#   ("enum", choices)        string from a closed set
#   ("bool",)                JSON true/false, never a truthy string or 1
#   ("int", lo, hi)          integer in [lo, hi]; hi None means unbounded
#   ("num", lo, hi)          int or float in [lo, hi]
#   ("list",)                a list, elements unconstrained
#   ("list_str",)            a list of non-blank strings
#   ("map_num", lo)          object mapping non-blank names to numbers >= lo
#   ("regex_list",)          list of strings that compile_safe_regex accepts
#   ("opt_int", lo)          null, or an integer >= lo
_POLICY_SPEC: dict = {
    "version": ("str",),
    "name": ("str",),
    "description": ("str",),
    "permissions": {
        "max_tier": ("enum", _TIERS),
        "require_confirmation_above": ("enum", _TIERS),
        "auto_approve_categories": ("list_str",),
    },
    "network": {
        "mode": ("enum", _NETWORK_MODES),
        "allowed_domains": ("list_str",),
        "blocked_domains": ("list_str",),
        "max_requests_per_minute": ("int", 0, None),
    },
    "filesystem": {
        # A blank entry resolves to the process CWD via
        # ``Path("").expanduser().resolve()``, which would hand the agent
        # the whole working directory. ("list_str",) rejects blanks.
        "read_paths": ("list_str",),
        "write_paths": ("list_str",),
        "blocked_paths": ("list_str",),
    },
    "hardware": {
        "sensors": {
            "allowed": ("list_str",),
            "blocked": ("list_str",),
            "max_read_rate_per_second": ("map_num", 0),
        },
        "actuators": {
            "allowed": ("list_str",),
            "blocked": ("list_str",),
            "requires_confirmation": ("list_str",),
            "max_actions_per_minute": ("int", 0, None),
        },
        "cameras": {
            "allowed": ("bool",),
            "max_captures_per_minute": ("int", 0, None),
            "auto_analyze": ("bool",),
            "store_frames": ("bool",),
        },
        "movement": {
            "max_speed_pct": ("int", 0, 100),
            # Shape is undocumented and nothing in the class reads it, so
            # this only pins "must be a list".
            "restricted_zones": ("list",),
            "emergency_stop_enabled": ("bool",),
            "requires_confirmation_above_speed": ("int", 0, 100),
        },
    },
    "skills": {
        "allow_generation": ("bool",),
        "require_approval": ("bool",),
        "max_pending": ("int", 0, None),
        "blocked_skill_ids": ("list_str",),
        "rate_limits": ("map_num", 0),
    },
    "memory": {
        "allow_persistent_storage": ("bool",),
        "allow_knowledge_graph": ("bool",),
        "max_notes": ("int", 0, None),
        "max_episodes": ("int", 0, None),
        "auto_forget_after_days": ("opt_int", 1),
    },
    "mcp": {
        "allow_external_servers": ("bool",),
        "allowed_servers": ("list_str",),
        "blocked_servers": ("list_str",),
        "max_concurrent_connections": ("int", 0, None),
    },
    "execution": {
        "max_tool_calls_per_turn": ("int", 1, None),
        "max_total_actions_per_session": ("int", 1, None),
        "timeout_per_action_ms": ("int", 1, None),
        "allow_shell_commands": ("bool",),
        "allow_file_write": ("bool",),
        "allow_network_requests": ("bool",),
        "denied_command_patterns": ("regex_list",),
    },
    "daemon": {
        "shell": {
            "allowed_commands": ("list_str",),
        },
        "applescript": {
            "max_length": ("int", 1, None),
            "denied_phrases": ("list_str",),
        },
    },
    "wasm": {
        "enabled": ("bool",),
        "memory_limit_mb": ("int", 1, None),
        "timeout_seconds": ("num", 0, None),
        "fuel_limit": ("int", 1, None),
        "allowed_host_functions": ("list_str",),
        "allowed_domains": ("list_str",),
    },
}


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _check_leaf(path: str, spec: tuple, value, errors: list[dict]) -> None:
    kind = spec[0]

    if kind == "str":
        if not isinstance(value, str) or not value.strip():
            errors.append({"field": path, "message": f"{path} must be a non-empty string, got {_type_name(value)}."})
        return

    if kind == "enum":
        choices = spec[1]
        if not isinstance(value, str) or value not in choices:
            errors.append({
                "field": path,
                "message": f"{path} must be one of {', '.join(choices)}; got {value!r}.",
            })
        return

    if kind == "bool":
        # ``isinstance(True, int)`` is True in Python, so the bool check has
        # to come first everywhere. Here it is the whole point: a JSON
        # string or number must not stand in for a security flag.
        if not isinstance(value, bool):
            errors.append({
                "field": path,
                "message": (
                    f"{path} must be the JSON boolean true or false, got "
                    f"{_type_name(value)} {value!r}. It is not coerced."
                ),
            })
        return

    if kind in ("int", "num"):
        lo, hi = spec[1], spec[2]
        ok = isinstance(value, int) and not isinstance(value, bool)
        if kind == "num":
            ok = ok or isinstance(value, float)
        if not ok:
            noun = "an integer" if kind == "int" else "a number"
            errors.append({"field": path, "message": f"{path} must be {noun}, got {_type_name(value)}."})
            return
        if lo is not None and value < lo:
            errors.append({"field": path, "message": f"{path} must be >= {lo}, got {value}."})
        if hi is not None and value > hi:
            errors.append({"field": path, "message": f"{path} must be <= {hi}, got {value}."})
        return

    if kind == "opt_int":
        if value is None:
            return
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append({"field": path, "message": f"{path} must be an integer or null, got {_type_name(value)}."})
            return
        if value < spec[1]:
            errors.append({"field": path, "message": f"{path} must be >= {spec[1]} or null, got {value}."})
        return

    if kind == "list":
        if not isinstance(value, list):
            errors.append({"field": path, "message": f"{path} must be an array, got {_type_name(value)}."})
        return

    if kind == "list_str":
        if not isinstance(value, list):
            errors.append({
                "field": path,
                "message": (
                    f"{path} must be an array of strings, got {_type_name(value)}. "
                    f"A non-array here reads back as an empty list, which "
                    f"silently drops the rule."
                ),
            })
            return
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                errors.append({
                    "field": f"{path}[{i}]",
                    "message": f"{path}[{i}] must be a non-blank string, got {_type_name(item)} {item!r}.",
                })
        return

    if kind == "map_num":
        lo = spec[1]
        if not isinstance(value, dict):
            errors.append({"field": path, "message": f"{path} must be an object, got {_type_name(value)}."})
            return
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                errors.append({"field": path, "message": f"{path} has a blank key."})
                continue
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                errors.append({
                    "field": f"{path}.{key}",
                    "message": f"{path}.{key} must be a number, got {_type_name(item)}.",
                })
                continue
            if item < lo:
                errors.append({"field": f"{path}.{key}", "message": f"{path}.{key} must be >= {lo}, got {item}."})
        return

    if kind == "regex_list":
        if not isinstance(value, list):
            errors.append({"field": path, "message": f"{path} must be an array of regex strings, got {_type_name(value)}."})
            return
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                errors.append({
                    "field": f"{path}[{i}]",
                    "message": f"{path}[{i}] must be a non-blank regex string, got {_type_name(item)}.",
                })
                continue
            try:
                compile_safe_regex(item, re.IGNORECASE)
            except (UnsafePatternError, re.error) as exc:
                # ``denied_command_patterns()`` drops a bad entry with a
                # log line and carries on, so an operator who saved one
                # would never learn their deny rule is not running.
                errors.append({
                    "field": f"{path}[{i}]",
                    "message": f"{path}[{i}] is not a usable deny pattern: {exc}",
                })
        return

    raise AssertionError(f"unknown policy spec kind {kind!r}")  # pragma: no cover


def _check_section(path: str, spec: dict, value, errors: list[dict]) -> None:
    if not isinstance(value, dict):
        errors.append({
            "field": path or "body",
            "message": f"{path or 'body'} must be a JSON object, got {_type_name(value)}.",
        })
        return

    for key, item in value.items():
        child = f"{path}.{key}" if path else key
        if key not in spec:
            errors.append({
                "field": child,
                "message": (
                    f"{child} is not a field SandboxPolicy reads. Unknown keys "
                    f"are ignored by the policy engine, so saving one would "
                    f"look like it applied when it did not. Known keys here: "
                    f"{', '.join(sorted(spec))}."
                ),
            })
            continue
        rule = spec[key]
        if isinstance(rule, dict):
            _check_section(child, rule, item, errors)
        else:
            _check_leaf(child, rule, item, errors)


def validate_policy_document(body) -> list[dict]:
    """Return a list of ``{"field", "message"}`` problems; empty means valid.

    Exported (rather than inlined into the handler) so tests and any future
    CLI-side policy linter assert the same rules the route enforces.
    """
    errors: list[dict] = []
    _check_section("", _POLICY_SPEC, body, errors)
    return errors


@router.get("/api/policy")
async def get_policy():
    if not state.policy:
        return {}
    return state.policy.to_dict()


@router.post("/api/policy/update")
async def update_policy(body: dict):
    """Replace the live sandbox policy, after validating it.

    ``SandboxPolicy(body)`` replaces ``_data`` wholesale, so this is a full
    document PUT wearing a POST. Validate, build, persist, and only then
    swap ``state.policy``. A disk failure must not leave the running brain
    on a policy that was never written down.
    """
    errors = validate_policy_document(body)
    if errors:
        first = errors[0]
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_policy",
                "field": first["field"],
                "message": (
                    first["message"]
                    if len(errors) == 1
                    else f"{first['message']} ({len(errors) - 1} more problem"
                         f"{'s' if len(errors) > 2 else ''} in this document.)"
                ),
                "errors": errors,
            },
        )

    candidate = SandboxPolicy(body)
    try:
        candidate.save()
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "policy_not_persisted",
                "field": "",
                "message": (
                    f"Policy validated but could not be written to disk: {exc}. "
                    f"The running policy is unchanged."
                ),
            },
        )
    state.policy = candidate
    return {"ok": True}


# ─────────────────────────────────────────────
# Workspace Folder Grants
# ─────────────────────────────────────────────
#
# Computer-use file tools refuse paths outside the policy's read/write
# lists. Operators grant explicit folders here (Desktop, Documents,
# project dirs) without globally widening the home directory.
# Persisted by ``SandboxPolicy.grant_folder`` to ``workspace_grants.json``.


def _grants_policy() -> SandboxPolicy:
    """Return the policy that owns folder grants. Falls back to a
    freshly-loaded one if the brain hasn't booted yet (CLI-style use)."""
    if state.policy is not None:
        return state.policy
    return SandboxPolicy.load_default()


@router.get("/api/security/grants")
async def list_workspace_grants():
    """List every folder grant currently honoured by the policy."""
    grants = _grants_policy().list_grants()
    return {"grants": grants}


@router.post("/api/security/grants")
async def grant_workspace_folder(body: dict):
    """Grant read/readwrite access to a folder.

    Body: ``{"path": "/Users/me/Desktop", "mode": "readwrite"}``.
    Mode defaults to ``read``; ``readwrite`` (or legacy ``write``) is
    required for ``computer_use__write_file`` / ``edit_file`` to succeed
    inside the folder.
    """
    path = (body or {}).get("path") or ""
    raw_mode = (body or {}).get("mode") or "read"
    mode = raw_mode.lower().strip()
    if mode not in ("read", "readwrite", "write"):
        return {"ok": False, "error": f"invalid mode: {raw_mode}"}
    if not path:
        return {"ok": False, "error": "path is required"}
    return _grants_policy().grant_folder(path, mode=mode)


@router.delete("/api/security/grants")
async def revoke_workspace_folder(path: str):
    """Revoke access to a previously granted folder by absolute path."""
    if not path:
        return {"ok": False, "error": "path is required"}
    removed = _grants_policy().revoke_folder(path)
    return {"ok": removed, "path": path}


# ─────────────────────────────────────────────
# Hardware Mesh API
# ─────────────────────────────────────────────


@router.post("/api/hardware/invoke")
async def hardware_invoke(body: dict):
    """Invoke a command on a connected node via the hardware mesh."""
    if not state.hardware_mesh:
        return {"error": "Hardware mesh not initialized"}
    return await state.hardware_mesh.invoke(
        node_id=body.get("node_id", ""),
        command=body.get("command", ""),
        params=body.get("params", {}),
        timeout=body.get("timeout", 10.0),
    )


@router.get("/api/hardware/mock_roomba")
async def mock_roomba_status():
    """Status of the brain-side mock Roomba (THESIS_SCENARIOS S5).

    Returns the mock's ``entity_id`` + ``is_running`` so the Lane 12
    Devices page can render the demo node. Returns
    ``{enabled: False}`` when the operator disabled the mock with
    ``FERAL_MOCK_ROOMBA=0``.
    """
    mock = getattr(state, "mock_roomba", None)
    if mock is None:
        return {"enabled": False}
    return mock.status()


@router.post("/api/hardware/mock_roomba/start")
async def mock_roomba_start(body: dict | None = None):
    """Start the brain-side mock Roomba.

    Body: ``{"entity_id": "vacuum.mock_roomba"}`` — optional; defaults
    to the mock's configured id. Returns the same structured shape as
    Lane 10's ``HomeAssistantIntegration.vacuum_start`` so the
    orchestrator's tool dispatch path can use either backend
    interchangeably:

    ``{success: True, data: {started: True, entity_id, service:
    "vacuum.start", duration_ms, simulated: True, note}}``

    ``simulated`` and ``note`` are the difference between shape parity
    and impersonation: the mock is enabled by default
    (``FERAL_MOCK_ROOMBA`` defaults to "1"), so without them a caller
    cannot distinguish "a vacuum started cleaning" from "there is no
    vacuum and nothing happened".

    Lane 11 SLA: < 500 ms on commodity hardware. Live verify in PR
    body asserts the elapsed time fits inside this budget.
    """
    mock = getattr(state, "mock_roomba", None)
    if mock is None:
        return {"success": False, "error": "mock_roomba disabled", "reason": "disabled"}
    entity_id = ""
    if isinstance(body, dict):
        entity_id = str(body.get("entity_id") or "")
    return await mock.start(entity_id=entity_id)


@router.post("/api/hardware/mock_roomba/stop")
async def mock_roomba_stop(body: dict | None = None):
    """Stop the brain-side mock Roomba. Parity with HA vacuum.stop."""
    mock = getattr(state, "mock_roomba", None)
    if mock is None:
        return {"success": False, "error": "mock_roomba disabled", "reason": "disabled"}
    entity_id = ""
    if isinstance(body, dict):
        entity_id = str(body.get("entity_id") or "")
    return await mock.stop(entity_id=entity_id)


@router.get("/api/hardware/mesh")
async def hardware_mesh_status():
    """Get hardware mesh status — connected daemons + discovered peripherals.

    Lane 12's Devices page consumes both fields:

    * ``nodes`` — daemons that registered over /v1/node and are still
      attached. Lane 12 lists them as the operator-actionable surface
      (rename / disconnect).
    * ``announced_devices`` — peripherals observed by any scanner-class
      daemon (typically the iOS companion app). Each entry carries
      ``device_id``, ``device_kind``, ``name``, ``manufacturer``,
      ``rssi_dbm``, ``advertised_services``, ``first_seen``,
      ``last_seen``, ``scanner_node_id``, ``metadata``. Lane 12 renders
      these grouped under their scanner so the operator sees "iPhone
      → AirPods, Apple Watch, …" without us inventing a separate
      grouping API.

    Both fields are always present (possibly empty) so the Lane 12
    client can do a single fetch.
    """
    if not state.hardware_mesh:
        return {"nodes": [], "announced_devices": []}
    return {
        "nodes": state.hardware_mesh.connected_nodes,
        "announced_devices": state.hardware_mesh.list_announced_devices(),
    }


def _serialize_capability(cap) -> dict:
    """JSON-safe capability + the generically-derived safety tier.

    The ``safety_tier`` / ``read_only`` / ``requires_approval`` come from the
    SAME generic resolver the LLM tool layer uses, so the companion's device
    card badges match exactly what the brain enforces."""
    from hardware.capability_skill import _safety_for

    try:
        tier, read_only, requires_approval = _safety_for(cap)
    except Exception:
        tier, read_only, requires_approval = "safe", False, False
    return {
        "id": cap.id,
        "name": cap.name,
        "description": cap.description,
        "category": cap.category,
        "permission_tier": cap.permission_tier,
        "action_type": getattr(cap, "action_type", None),
        "safety_tier": tier,
        "read_only": read_only,
        "requires_approval": requires_approval,
        "requires_confirmation": cap.requires_confirmation,
        "reversible": cap.reversible,
        "rate_limit_per_minute": cap.rate_limit_per_minute,
        "params": list(cap.parameters or []),
        "returns": cap.returns,
        "has_verify": isinstance(getattr(cap, "verify", None), dict),
    }


def _serialize_fleet_device(manifest, verification) -> dict:
    return {
        "device_id": manifest.device_id,
        "device_type": manifest.device_type,
        "name": manifest.name,
        "manufacturer": manifest.manufacturer,
        "model": manifest.model,
        "connection_type": manifest.connection_type,
        "location": manifest.location,
        "battery_powered": manifest.battery_powered,
        "tags": list(manifest.tags or []),
        "sensors": list(manifest.sensors or []),
        "actuators": list(manifest.actuators or []),
        "capabilities": [_serialize_capability(c) for c in manifest.capabilities],
        "last_verified": verification,
    }


@router.get("/api/hardware/fleet")
async def hardware_fleet():
    """Unified, server-driven fleet view for the companion app.

    One fetch returns every self-describing device the brain knows about —
    its full manifest (capabilities, params, generically-derived safety
    tiers, verify contracts), plus each device's last action+verify outcome
    (the live "honesty loop" state), mesh-announced peripherals, and connected
    nodes. The companion renders device cards directly from this; a newly
    registered device appears with no app change.
    """
    registry = state.device_registry
    if registry is None:
        return {
            "devices": [],
            "verifications": {},
            "mesh": {"nodes": [], "announced_devices": []},
            "stats": {},
            "primary_session_id": getattr(state, "primary_session_id", None),
        }

    verifications = {}
    try:
        verifications = registry.all_verifications()
    except Exception:
        verifications = {}

    devices = []
    for manifest in registry.list_devices():
        devices.append(
            _serialize_fleet_device(manifest, verifications.get(manifest.device_id))
        )

    mesh = {"nodes": [], "announced_devices": []}
    if state.hardware_mesh:
        try:
            mesh = {
                "nodes": state.hardware_mesh.connected_nodes,
                "announced_devices": state.hardware_mesh.list_announced_devices(),
            }
        except Exception:
            pass

    stats = {}
    try:
        stats = registry.stats
    except Exception:
        stats = {}

    return {
        "devices": devices,
        "verifications": verifications,
        "mesh": mesh,
        "stats": stats,
        "primary_session_id": getattr(state, "primary_session_id", None),
    }
