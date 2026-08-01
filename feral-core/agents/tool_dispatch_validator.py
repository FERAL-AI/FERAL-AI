"""
Pre-dispatch validation: manifest JSON schema + backend dispatch contract.

Every LLM tool call is checked against the skill manifest and the backing
implementation's dispatch table before SkillExecutor runs. Manifest drift
is caught at boot (ToolDispatchValidator precomputes schemas) and in CI
(test_manifest_dispatch_contract.py).
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from models.skill_manifest import EndpointParam, SkillEndpoint, SkillManifest

logger = logging.getLogger("feral.tool_dispatch_validator")

MANIFEST_ROOT = Path(__file__).resolve().parent.parent / "skills" / "manifests"

# Integration backends registered at brain boot (state.py). Used for contract
# introspection when the impl registry has not been wired yet (CI / unit tests).
INTEGRATION_BACKENDS: dict[str, tuple[str, str]] = {
    "smart_home_hue": ("integrations.home_assistant", "HomeAssistantIntegration"),
    "messaging_sms": ("integrations.messaging", "MessagingHub"),
    "calendar_google": ("integrations.calendar", "CalendarIntegration"),
    "spotify_music": ("integrations.spotify", "SpotifyIntegration"),
    "email": ("integrations.email", "EmailIntegration"),
    "google_drive": ("integrations.google_drive", "GoogleDriveIntegration"),
    "google_contacts": ("integrations.google_contacts", "GoogleContactsIntegration"),
    "microsoft365": ("integrations.microsoft365", "Microsoft365Integration"),
    "notion": ("integrations.notion", "NotionIntegration"),
    "health_data": ("integrations.health_platforms", "HealthAggregator"),
}

# Executor lanes that do not use a Python dispatch table.
HTTP_LANE = "http"
DAEMON_LANE = "daemon"
WS_EXECUTE_LANE = "ws_execute"
PYTHON_LANE = "python"
ORCHESTRATOR_LANE = "orchestrator"

DAEMON_ARG_NAMES = frozenset({"script", "command", "timeout_seconds"})

JSON_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


@dataclass
class ValidationResult:
    ok: bool
    error_code: Optional[str] = None
    reason: Optional[str] = None
    fixed_args: Optional[dict[str, Any]] = None
    schema_hint: Optional[dict[str, Any]] = None


@dataclass
class EndpointSchema:
    skill_id: str
    endpoint_id: str
    endpoint: SkillEndpoint
    lane: str
    dispatch_keys: set[str] = field(default_factory=set)
    backend_param_names: set[str] = field(default_factory=set)
    uses_args_dict: bool = False


def _parse_dispatch_keys_from_source(source: str) -> set[str]:
    """Extract endpoint ids from execute() source (dispatch dict + if/elif chains)."""
    keys: set[str] = set()
    in_dispatch = False
    brace_depth = 0
    for line in source.splitlines():
        stripped = line.strip()
        if not in_dispatch:
            if re.match(r"dispatch\s*=\s*\{", stripped):
                in_dispatch = True
                brace_depth = stripped.count("{") - stripped.count("}")
                m = re.search(r'"([^"]+)"\s*:', stripped)
                if m:
                    keys.add(m.group(1))
                if brace_depth <= 0:
                    in_dispatch = False
                continue
        else:
            brace_depth += stripped.count("{") - stripped.count("}")
            m = re.match(r'"([^"]+)"\s*:', stripped)
            if m:
                keys.add(m.group(1))
            if brace_depth <= 0:
                break
    for m in re.finditer(r"""endpoint_id\s*==\s*['"]([^'"]+)['"]""", source):
        keys.add(m.group(1))
    for m in re.finditer(r'"([^"]+)"\s*:\s*self\.\w+', source):
        keys.add(m.group(1))
    for m in re.finditer(r"""endpoint_id\s+not\s+in\s+\(([^)]+)\)""", source):
        for sm in re.finditer(r"""['"]([^'"]+)['"]""", m.group(1)):
            keys.add(sm.group(1))
    return keys


def _parse_module_constant_keys(module_path: str, const_name: str) -> set[str]:
    try:
        mod = importlib.import_module(module_path)
        value = getattr(mod, const_name, None)
        if isinstance(value, dict):
            return {str(k) for k in value.keys()}
    except Exception:
        pass
    return set()


def _handler_accepts_args_dict(handler: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return False
    params = [
        p for p in sig.parameters.values()
        if p.name not in ("self", "cls")
    ]
    if len(params) == 1 and params[0].name in ("args", "params"):
        return True
    return False


def _callable_param_names(handler: Callable[..., Any]) -> set[str]:
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return set()
    names: set[str] = set()
    for param in sig.parameters.values():
        if param.name in ("self", "cls", "args", "params", "vault", "kwargs"):
            continue
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return set()  # **kwargs accepts anything; skip strict name check
        names.add(param.name)
    return names


def _resolve_handler_for_key(
    cls: type,
    source: str,
    endpoint_id: str,
) -> Optional[Callable[..., Any]]:
    pattern = rf'"{re.escape(endpoint_id)}"\s*:\s*self\.(\w+)'
    m = re.search(pattern, source)
    if m:
        return getattr(cls, m.group(1), None)
    nested = rf'"{re.escape(endpoint_id)}"\s*:\s*self\.(\w+)\.(\w+)'
    m2 = re.search(nested, source)
    if not m2:
        return None
    bridge_attr = getattr(cls(), m2.group(1), None)
    if bridge_attr is None:
        return None
    return getattr(bridge_attr, m2.group(2), None)


def _infer_execution_lane(endpoint: SkillEndpoint) -> str:
    url = getattr(endpoint, "url", "") or ""
    if not isinstance(url, str):
        url = ""
    if endpoint.method == "WS_EXECUTE":
        return WS_EXECUTE_LANE
    if url.startswith("daemon://"):
        return DAEMON_LANE
    if url.startswith("python://"):
        return PYTHON_LANE
    if url.startswith("orchestrator://"):
        return ORCHESTRATOR_LANE
    return HTTP_LANE


def _coerce_default(param: EndpointParam) -> Any:
    if param.default is None:
        return None
    raw = param.default
    if param.type == "boolean":
        return str(raw).lower() in ("true", "1", "yes")
    if param.type == "integer":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if param.type == "number":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def _schema_hint_for_endpoint(endpoint: SkillEndpoint) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in endpoint.params:
        prop: dict[str, Any] = {
            "type": param.type,
            "description": param.description,
        }
        if param.enum:
            prop["enum"] = param.enum
        if param.default is not None:
            prop["default"] = _coerce_default(param)
        if param.items:
            prop["items"] = param.items
        properties[param.name] = prop
        if param.required:
            required.append(param.name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


class ToolDispatchValidator:
    """Validate tool calls against manifest schema + backend dispatch tables."""

    def __init__(
        self,
        registry: Any = None,
        manifests: Optional[dict[str, SkillManifest]] = None,
    ):
        self._registry = registry
        # Only a validator built *from* a registry tracks that registry's
        # generation. An explicit ``manifests=`` snapshot is the caller's
        # own fixed view and must never be silently replaced.
        self._tracks_registry = manifests is None and registry is not None
        if manifests is not None:
            self._manifests = dict(manifests)
        elif registry is not None and hasattr(registry, "skills"):
            self._manifests = dict(registry.skills)
        else:
            self._manifests = self._load_manifest_files()
        self._registry_generation = self._current_registry_generation()
        self._endpoint_schemas: dict[tuple[str, str], EndpointSchema] = {}
        self._precompute_schemas()

    def _current_registry_generation(self) -> Optional[int]:
        """Read ``SkillRegistry.generation``, or ``None`` when the registry
        does not expose one (plain dicts, mocks, manifest-only validators)."""
        if not self._tracks_registry:
            return None
        generation = getattr(self._registry, "generation", None)
        return generation if isinstance(generation, int) else None

    def refresh_if_stale(self) -> bool:
        """Re-snapshot manifests and schemas when the registry has changed.

        ``__init__`` takes a one-shot copy of ``registry.skills`` and
        precomputes every endpoint schema, so skills registered afterwards
        (marketplace installs, ``SkillRegistry.reload_skill``, Tool Genesis
        output, hot-plugged hardware) used to validate as
        ``unknown_endpoint`` forever. Callers that hold a long-lived
        validator call this before ``validate()``.

        Returns ``True`` when a rebuild happened.
        """
        if not self._tracks_registry:
            return False
        generation = self._current_registry_generation()
        if generation is None or generation == self._registry_generation:
            return False
        if not hasattr(self._registry, "skills"):
            return False
        self._manifests = dict(self._registry.skills)
        self._registry_generation = generation
        self._endpoint_schemas = {}
        self._precompute_schemas()
        logger.info(
            "ToolDispatchValidator refreshed at registry generation %s (%d skills)",
            generation, len(self._manifests),
        )
        return True

    @staticmethod
    def _load_manifest_files() -> dict[str, SkillManifest]:
        manifests: dict[str, SkillManifest] = {}
        if not MANIFEST_ROOT.exists():
            return manifests
        for path in sorted(MANIFEST_ROOT.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                manifest = SkillManifest(**data)
                manifests[manifest.skill_id] = manifest
            except Exception as exc:
                logger.warning("Failed to load manifest %s: %s", path.name, exc)
        return manifests

    def _precompute_schemas(self) -> None:
        for skill_id, manifest in self._manifests.items():
            has_python_backend = self._find_execute_callable(skill_id) is not None
            backend_keys, backend_params, uses_args_dict, _ = (
                self._resolve_backend_contract(skill_id)
            )
            for endpoint in manifest.endpoints:
                if skill_id == "subagent":
                    lane = ORCHESTRATOR_LANE
                elif has_python_backend:
                    lane = PYTHON_LANE
                else:
                    lane = _infer_execution_lane(endpoint)
                schema = EndpointSchema(
                    skill_id=skill_id,
                    endpoint_id=endpoint.id,
                    endpoint=endpoint,
                    lane=lane,
                    dispatch_keys=backend_keys,
                    backend_param_names=backend_params.get(endpoint.id, set()),
                    uses_args_dict=uses_args_dict.get(endpoint.id, False),
                )
                if lane == DAEMON_LANE:
                    schema.backend_param_names = set(DAEMON_ARG_NAMES)
                elif lane == HTTP_LANE and not backend_keys:
                    schema.backend_param_names = {p.name for p in endpoint.params}
                self._endpoint_schemas[(skill_id, endpoint.id)] = schema

    def _resolve_backend_contract(
        self, skill_id: str,
    ) -> tuple[set[str], dict[str, set[str]], dict[str, bool], dict[str, str]]:
        dispatch_keys: set[str] = set()
        param_names: dict[str, set[str]] = {}
        uses_args_dict: dict[str, bool] = {}

        execute_fn = self._find_execute_callable(skill_id)
        if execute_fn is None:
            return dispatch_keys, param_names, uses_args_dict, {}

        try:
            source = inspect.getsource(execute_fn)
        except (OSError, TypeError):
            source = ""

        dispatch_keys = _parse_dispatch_keys_from_source(source)
        if skill_id == "desktop_automation":
            dispatch_keys |= _parse_module_constant_keys(
                "skills.impl.desktop_automation", "_ENDPOINT_TO_FERAL_ACTION",
            )

        owner_cls = getattr(execute_fn, "__self__", None)
        owner_type = owner_cls.__class__ if owner_cls is not None else None
        if owner_type is None:
            qual = getattr(execute_fn, "__qualname__", "").split(".")[0]
            mod = inspect.getmodule(execute_fn)
            if mod and qual:
                owner_type = getattr(mod, qual, None)

        for key in dispatch_keys:
            handler: Optional[Callable[..., Any]] = None
            if owner_type is not None and source:
                handler = _resolve_handler_for_key(owner_type, source, key)
            if handler is None and owner_cls is not None:
                handler = getattr(owner_cls, key, None)
            if handler is not None:
                if _handler_accepts_args_dict(handler):
                    uses_args_dict[key] = True
                    param_names[key] = set()
                else:
                    uses_args_dict[key] = False
                    param_names[key] = _callable_param_names(handler)
            else:
                uses_args_dict[key] = True
                param_names[key] = set()

        return dispatch_keys, param_names, uses_args_dict, {}

    def _find_execute_callable(self, skill_id: str) -> Optional[Callable[..., Any]]:
        impl = None
        try:
            from skills.impl import get_implementation
            impl = get_implementation(skill_id)
        except Exception:
            impl = None
        if impl is not None and hasattr(impl, "execute"):
            return impl.execute

        if skill_id in INTEGRATION_BACKENDS:
            module_path, class_name = INTEGRATION_BACKENDS[skill_id]
            try:
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                instance = cls()
                return instance.execute
            except Exception as exc:
                logger.debug("Integration backend lookup failed for %s: %s", skill_id, exc)
        return None

    def get_endpoint_schema(self, skill_id: str, endpoint_id: str) -> Optional[EndpointSchema]:
        return self._endpoint_schemas.get((skill_id, endpoint_id))

    def validate(
        self,
        skill_id: str,
        endpoint_id: str,
        args: Optional[dict[str, Any]],
    ) -> ValidationResult:
        manifest = self._manifests.get(skill_id)
        if manifest is None:
            return ValidationResult(
                ok=False,
                error_code="unknown_endpoint",
                reason=f"Unknown skill: {skill_id}",
                schema_hint=None,
            )

        endpoint = next((ep for ep in manifest.endpoints if ep.id == endpoint_id), None)
        if endpoint is None:
            return ValidationResult(
                ok=False,
                error_code="unknown_endpoint",
                reason=f"Endpoint '{endpoint_id}' is not declared in skill '{skill_id}'",
                schema_hint=None,
            )

        schema = self._endpoint_schemas.get((skill_id, endpoint_id))
        if schema is None:
            schema = EndpointSchema(
                skill_id=skill_id,
                endpoint_id=endpoint_id,
                endpoint=endpoint,
                lane=_infer_execution_lane(endpoint),
            )

        hint = _schema_hint_for_endpoint(endpoint)

        if schema.lane == PYTHON_LANE:
            if schema.dispatch_keys and endpoint_id not in schema.dispatch_keys:
                return ValidationResult(
                    ok=False,
                    error_code="unknown_endpoint",
                    reason=(
                        f"Endpoint '{endpoint_id}' is declared in the manifest for "
                        f"'{skill_id}' but is not implemented in the backend dispatch table "
                        f"(available: {sorted(schema.dispatch_keys)})."
                    ),
                    schema_hint=hint,
                )
            if not schema.dispatch_keys and self._find_execute_callable(skill_id) is None:
                return ValidationResult(
                    ok=False,
                    error_code="unknown_endpoint",
                    reason=f"No Python backing implementation registered for '{skill_id}'",
                    schema_hint=hint,
                )

        working = dict(args or {})
        fixed: dict[str, Any] = {}

        for param in endpoint.params:
            if param.name in working:
                fixed[param.name] = working[param.name]
            elif param.default is not None:
                fixed[param.name] = _coerce_default(param)
            elif param.required:
                return ValidationResult(
                    ok=False,
                    error_code="missing_required_field",
                    reason=(
                        f"Missing required parameter '{param.name}' for "
                        f"{skill_id}__{endpoint_id}."
                    ),
                    schema_hint=hint,
                )

        if schema.lane == DAEMON_LANE:
            if not fixed.get("script") and not fixed.get("command"):
                return ValidationResult(
                    ok=False,
                    error_code="missing_required_field",
                    reason=(
                        f"daemon:// endpoint '{endpoint_id}' requires 'command' or 'script'."
                    ),
                    schema_hint=hint,
                )

        for param in endpoint.params:
            if param.name not in fixed:
                continue
            value = fixed[param.name]
            if value is None:
                continue
            checker = JSON_TYPE_CHECKS.get(param.type)
            if checker and not checker(value):
                return ValidationResult(
                    ok=False,
                    error_code="invalid_args",
                    reason=(
                        f"Parameter '{param.name}' must be of type '{param.type}', "
                        f"got {type(value).__name__}."
                    ),
                    schema_hint=hint,
                )

        if (
            schema.dispatch_keys
            and endpoint_id in schema.dispatch_keys
            and not schema.uses_args_dict
            and schema.backend_param_names
        ):
            manifest_names = {p.name for p in endpoint.params}
            missing_backend = manifest_names - schema.backend_param_names
            if missing_backend and schema.lane == PYTHON_LANE:
                return ValidationResult(
                    ok=False,
                    error_code="invalid_args",
                    reason=(
                        f"Manifest params {sorted(missing_backend)} are not accepted by "
                        f"backend handler for '{endpoint_id}' "
                        f"(accepts: {sorted(schema.backend_param_names)})."
                    ),
                    schema_hint=hint,
                )

        return ValidationResult(ok=True, fixed_args=fixed, schema_hint=hint)

    def contract_violations(self, skill_id: str, endpoint_id: str) -> list[str]:
        """CI-facing manifest↔backend drift report for one endpoint."""
        violations: list[str] = []
        manifest = self._manifests.get(skill_id)
        if manifest is None:
            return [f"{skill_id}: manifest not loaded"]

        endpoint = next((ep for ep in manifest.endpoints if ep.id == endpoint_id), None)
        if endpoint is None:
            return [f"{skill_id}.{endpoint_id}: endpoint missing from manifest"]

        schema = self._endpoint_schemas.get((skill_id, endpoint_id))
        if schema is None:
            return [f"{skill_id}.{endpoint_id}: schema not precomputed"]

        if schema.lane in (HTTP_LANE, DAEMON_LANE, WS_EXECUTE_LANE, ORCHESTRATOR_LANE):
            return violations

        if schema.lane == PYTHON_LANE:
            if not schema.dispatch_keys:
                violations.append(
                    f"{skill_id}.{endpoint_id}: no backend dispatch table found"
                )
            elif endpoint_id not in schema.dispatch_keys:
                violations.append(
                    f"{skill_id}.{endpoint_id}: endpoint id missing from backend "
                    f"(have {sorted(schema.dispatch_keys)})"
                )
            elif (
                not schema.uses_args_dict
                and schema.backend_param_names
            ):
                for param in endpoint.params:
                    if param.name not in schema.backend_param_names:
                        violations.append(
                            f"{skill_id}.{endpoint_id}: manifest param '{param.name}' "
                            f"not in backend signature {sorted(schema.backend_param_names)}"
                        )
        elif schema.lane == DAEMON_LANE:
            for param in endpoint.params:
                if param.name not in DAEMON_ARG_NAMES:
                    violations.append(
                        f"{skill_id}.{endpoint_id}: daemon param '{param.name}' "
                        f"not accepted by executor"
                    )

        return violations


def make_tool_error_envelope(
    *,
    tool_call_id: str,
    error_code: str,
    reason: str,
    schema_hint: Optional[dict[str, Any]] = None,
    fixable_by_llm: bool = True,
) -> dict[str, Any]:
    """Structured error the LLM can read and retry against."""
    return {
        "success": False,
        "is_error": True,
        "tool_call_id": tool_call_id,
        "error_code": error_code,
        "reason": reason,
        "error": reason,
        "fixable_by_llm": fixable_by_llm,
        "schema_hint": schema_hint or {},
    }
