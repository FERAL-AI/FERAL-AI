"""
@feral_tool — Decorator to mark a method as a FERAL agent tool.

Usage::

    class MyPlugin(FeralPlugin):

        @feral_tool(
            name="search_web",
            description="Search the web for information",
            parameters={"query": {"type": "string", "description": "Search query"}}
        )
        async def search(self, query: str) -> dict:
            ...
"""

from __future__ import annotations
import inspect
import json
from copy import deepcopy
from typing import Any, Callable, get_origin, get_type_hints


def feral_tool(
    name: str | None = None,
    description: str = "",
    parameters: dict[str, dict] | None = None,
) -> Callable:
    """Decorate an async method to register it as a FERAL tool.

    Args:
        name: Tool name (defaults to method name).
        description: Human-readable description shown to the LLM.
        parameters: Dict of param_name -> {type, description, required}.
            If omitted, inferred from type hints.
    """

    def decorator(fn: Callable) -> Callable:
        if not inspect.iscoroutinefunction(fn):
            raise ValueError("FERAL tools must be async methods")
        resolved_params = deepcopy(parameters)
        if resolved_params is None:
            resolved_params = _infer_parameters(fn)

        if not isinstance(resolved_params, dict):
            raise ValueError("parameters must be an object")
        if len(resolved_params) > 64:
            raise ValueError("At most 64 tool parameters are supported")
        for pname, info in resolved_params.items():
            if not isinstance(pname, str) or not pname.isidentifier() or not isinstance(info, dict):
                raise ValueError("Invalid tool parameter")
            if info.get("type", "string") not in ("string", "integer", "number", "boolean", "array", "object"):
                raise ValueError(f"Unsupported parameter type for {pname}")
            if not isinstance(info.get("description", ""), str):
                raise ValueError(f"description must be text for {pname}")
            if "required" in info and not isinstance(info["required"], bool):
                raise ValueError(f"required must be boolean for {pname}")
            if "default" in info:
                try:
                    json.dumps(info["default"], allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"default must be JSON for {pname}") from exc
        fn._feral_tool_meta = {
            "name": name or fn.__name__,
            "description": description or fn.__doc__ or "",
            "parameters": resolved_params,
        }
        return fn

    return decorator


def _infer_parameters(fn: Callable) -> dict[str, dict[str, Any]]:
    """Infer tool parameters from function type hints."""
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except (NameError, TypeError) as exc:
        raise ValueError("Unresolved tool type hints; supply explicit parameters") from exc
    params: dict[str, dict] = {}
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls", "vault", "kwargs"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD,
                          inspect.Parameter.POSITIONAL_ONLY):
            raise ValueError("Tools require named parameters without *args/**kwargs")
        ptype = hints.get(pname, str)
        ptype = get_origin(ptype) or ptype
        type_map = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
        if ptype not in type_map:
            raise ValueError(f"Unsupported type hint for {pname}; supply explicit parameters")
        params[pname] = {
            "type": type_map[ptype],
            "description": "",
            "required": param.default is inspect.Parameter.empty,
        }
        if param.default is not inspect.Parameter.empty:
            params[pname]["default"] = deepcopy(param.default)
    return params
