"""Standalone SDK validation and real-loader integration via a disposable process."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SDK = Path(__file__).resolve().parents[1]
REPO = SDK.parents[1]
sys.path.insert(0, str(SDK))
from feral_sdk import FeralPlugin, feral_tool


class Calculator(FeralPlugin):
    name = "sdk_unit_calculator"

    @feral_tool(description="Add integers")
    async def add(self, a: int, b: int = 29) -> dict:
        return {"sum": a + b}


def test_future_annotations_defaults_manifest_and_execution():
    plugin = Calculator()
    manifest = plugin.to_manifest()
    ep = manifest["endpoints"][0]
    assert ep["method"] == "PYTHON" and ep["url"] == ""
    assert ep["requires_user_approval"] is True and ep["safety_tier"] == "confirm"
    assert ep["params"] == [
        {"name": "a", "type": "integer", "description": "", "required": True},
        {"name": "b", "type": "integer", "description": "", "required": False, "default": 29},
    ]
    assert asyncio.run(plugin.execute("add", {"a": 13}, {}))["data"] == {"sum": 42}
    ep["params"][1]["default"] = 100
    plugin.tools["add"]["meta"]["parameters"]["b"]["default"] = 500
    assert plugin.to_manifest()["endpoints"][0]["params"][1]["default"] == 29


def test_unknown_and_handler_failure_envelopes():
    plugin = Calculator()
    assert asyncio.run(plugin.execute("missing", {}, {}))["status_code"] == 404
    result = asyncio.run(plugin.execute("add", {}, {}))
    assert result["success"] is False and result["status_code"] == 500


def test_duplicate_names_refused():
    class Duplicate(FeralPlugin):
        name = "duplicate"

        @feral_tool(name="same")
        async def first(self):
            return 1

        @feral_tool(name="same")
        async def second(self):
            return 2
    with pytest.raises(ValueError, match="Duplicate"):
        Duplicate()


@pytest.mark.parametrize("name", ["../bad", "contains__delimiter", "", "x" * 65, 42])
def test_bad_explicit_names(name):
    class Bad(Calculator):
        pass
    Bad.name = name
    # Empty is the existing automatic class-name convention, not an invalid explicit ID.
    if name == "":
        assert Bad().name == "bad"
    else:
        with pytest.raises(ValueError, match="plugin name"):
            Bad()


def test_sync_tools_and_unresolved_annotations_refused():
    with pytest.raises(ValueError, match="async"):
        feral_tool()(lambda x: x)

    async def unresolved(a):
        return a
    unresolved.__annotations__["a"] = "NonexistentType"
    with pytest.raises(ValueError, match="explicit parameters"):
        feral_tool()(unresolved)


@pytest.mark.parametrize("parameters", [[], {"a": {"type": "binary"}}, {"a": {"required": 1}},
                                         {"a": {"default": float("nan")}}, {"a": {"default": object()}},
                                         {"bad-name": {"type": "integer"}}])
def test_malformed_explicit_parameters(parameters):
    async def fn(a):
        return a
    with pytest.raises(ValueError):
        feral_tool(parameters=parameters)(fn)


def test_explicit_parameters_are_detached():
    params = {"a": {"type": "array", "required": False, "default": [1]}}
    class Lists(FeralPlugin):
        name = "lists"

        @feral_tool(parameters=params)
        async def rows(self, a=None):
            return a
    params["a"]["default"].append(2)
    assert Lists().to_manifest()["endpoints"][0]["params"][0]["default"] == [1]


def test_standalone_import_does_not_import_runtime_and_factory_errors_explicitly(tmp_path):
    script = '''
import importlib.abc, sys
class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "skills" or name.startswith("skills."):
            raise ModuleNotFoundError("Runtime intentionally absent", name=name)
sys.meta_path.insert(0, BlockRuntime())
from feral_sdk import FeralPlugin, feral_tool
class Example(FeralPlugin):
    name = "standalone"
    @feral_tool()
    async def add(self, a: int): return a
assert Example().to_manifest()["endpoints"][0]["url"] == ""
assert not any(name == "skills" or name.startswith("skills.") for name in sys.modules)
try: Example.runtime_skill()
except RuntimeError as e: assert "FERAL runtime" in str(e)
else: raise AssertionError("Missing dependency did not fail")
'''
    env = dict(os.environ, PYTHONPATH=str(SDK), FERAL_HOME=str(tmp_path / "home"), FERAL_DATA_HOME=str(tmp_path / "data"))
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_actual_registry_executor_review_walkthrough(tmp_path):
    env = dict(os.environ, FERAL_HOME=str(tmp_path / "home"), FERAL_DATA_HOME=str(tmp_path / "data"))
    result = subprocess.run([sys.executable, str(REPO / "examples/sdk-authoring/walkthrough.py")],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.splitlines()[-1])
    assert observed == {"contract": "sdk-python-authoring-v1", "validated": True,
                        "registered_via_existing_route": True, "denied_execution_count": 0,
                        "reviewed_sum": 42, "executions": 1, "single_use_review": True,
                        "foreign_session_review_refused": True, "invalid_arguments_refused": True,
                        "bounds_refused": True, "uninstalled": True, "stale_review_no_execution": True}


def test_bare_plugin_reload_is_refused_without_python_backing(tmp_path):
    result = subprocess.run([sys.executable, str(REPO / "examples/sdk-authoring/loader_negative_check.py")],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "reload_acknowledged": False, "backing_implementation_present": False,
        "handler_executed": False, "invalid_adapter_refused": True}


def test_container_and_boolean_hints():
    async def tools(a: list[int], b: dict[str, int], c: bool, d: float):
        return a, b, c, d
    marked = feral_tool()(tools)
    assert [p["type"] for p in marked._feral_tool_meta["parameters"].values()] == ["array", "object", "boolean", "number"]
