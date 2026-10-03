"""Actual loader publication, failure retention and cancelled-thread recovery.

Known read-only disposable packages only. No accounts, model or runtime startup.
"""
from __future__ import annotations

import asyncio
from contextvars import copy_context
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from models.skill_manifest import SkillManifest
from skills.base import BaseSkill
from skills.impl import capture_registrations, get_implementation, register_instance
from skills.registry import SkillRegistry

SID = "reload_fixture"


def write_package(home: Path, *, source: str | None, method="PYTHON", skill_id=SID,
                  location="skills", description="fixture") -> Path:
    directory = home / location / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"skill_id": skill_id, "description": description,
                "brand": {"name": "Reload fixture"}, "endpoints": [
                    {"id": "read", "method": method, "url": "" if method == "PYTHON" else "http://127.0.0.1:1/read",
                     "description": "Known read-only fixture", "params": []}]}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    path = directory / "impl.py"
    if source is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(source)
    return directory


def source(value=1, *, decorate=False):
    return ("from skills.base import BaseSkill\n"
            + ("from skills.impl import register_skill\n@register_skill\n" if decorate else "")
            + "class Fixture(BaseSkill):\n"
              f"    def __init__(self): super().__init__({SID!r}); self.calls = 0\n"
              "    async def execute(self, endpoint_id, args, vault):\n"
              "        self.calls += 1\n"
              f"        return {{'success': True, 'status_code': 200, 'data': {{'value': {value}}}}}\n")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path / "data"))
    yield tmp_path
    for name, module in tuple(sys.modules.items()):
        if name == f"feral_skill_{SID}" or (name.startswith("_feral_skill_staging_")
                and module is not None and str(tmp_path) in str(getattr(module, "__file__", ""))):
            sys.modules.pop(name, None)


def snapshot(registry):
    return (registry.skills.get(SID), registry._tool_cache.get(SID),
            get_implementation(SID), sys.modules.get(f"feral_skill_{SID}"), registry.generation)


@pytest.mark.parametrize("broken,code", [
    (None, "missing_implementation"),
    ("class Bare: pass", "invalid_implementation"),
    ("not valid python =", "implementation_failed"),
    ("import nonexistent_feral_fixture_dependency", "implementation_failed"),
    (source() + "raise RuntimeError('PRIVATE_CREDENTIAL_SOURCE_MARKER')", "implementation_failed"),
    (source().replace("self.calls = 0", "raise RuntimeError('PRIVATE_CREDENTIAL_SOURCE_MARKER')"), "implementation_failed"),
    (source().replace(SID, "foreign_fixture"), "identity_mismatch"),
    (source().replace("async def execute", "def execute"), "invalid_implementation"),
    ("from skills.base import BaseSkill\nclass Fixture(BaseSkill):\n    def __init__(self): super().__init__('reload_fixture')", "invalid_implementation"),
    (source() + "\nclass Other(Fixture): pass", "invalid_implementation"),
    (source(decorate=True) + "\nraise RuntimeError('PRIVATE_CREDENTIAL_SOURCE_MARKER')", "implementation_failed"),
    (source() + "\nfrom skills.impl import register_instance\nregister_instance('foreign_fixture', Fixture())", "implementation_failed"),
])
@pytest.mark.asyncio
async def test_failed_replacement_retains_exact_old_live_state(home, broken, code):
    write_package(home, source=source())
    registry = SkillRegistry()
    assert registry.reload_skill(SID)
    before = snapshot(registry)
    routines = Mock()
    registry._auto_create_routines = routines
    write_package(home, source=broken, description="broken replacement")
    ok, actual, reason = await registry.reload_skill_detail_async(SID)
    assert not ok and actual == code and reason
    assert "PRIVATE_CREDENTIAL_SOURCE_MARKER" not in reason
    assert all(left is right for left, right in zip(snapshot(registry)[:-1], before[:-1]))
    assert registry.generation == before[-1]
    routines.assert_not_called()
    old = registry.get_skill(SID)
    assert old is before[2]
    assert (await old.execute("read", {}, {}))["data"] == {"value": 1}
    assert len([name for name in sys.modules if name.startswith("_feral_skill_staging_")
                and str(home) in str(getattr(sys.modules[name], "__file__", ""))]) == 1


@pytest.mark.asyncio
async def test_exact_alias_decorator_and_sdk_factory_exports(home, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "sdk/python"))
    for body in [source(decorate=True) + "\nAlias = Fixture\n",
                 "from feral_sdk import FeralPlugin, feral_tool\n"
                 "class Authored(FeralPlugin):\n    name = 'reload_fixture'\n"
                 "    @feral_tool()\n    async def read(self): return 42\n"
                 "Runtime = Authored.runtime_skill()\nAlias = Runtime\n"]:
        write_package(home, source=body)
        registry = SkillRegistry()
        assert await registry.reload_skill_detail_async(SID) == (True, "", "")
        implementation = registry.get_skill(SID)
        assert isinstance(implementation, BaseSkill) and implementation.skill_id == SID
        assert sys.modules[f"feral_skill_{SID}"] is sys.modules[type(implementation).__module__] or type(implementation).__module__ == "feral_sdk.plugin"


def test_selected_broken_source_never_falls_through_to_valid_generated(home):
    write_package(home, source="bad python =")
    write_package(home, source=source(7), location="skills/generated")
    registry = SkillRegistry()
    assert registry.reload_skill_detail(SID)[0] is False
    assert SID not in registry.skills and get_implementation(SID) is None


@pytest.mark.parametrize("identifier", ["../escape", "x/y", "x\\y", " white", "control\n", "", "a" * 129])
def test_unsafe_ids_refused_before_path_access(home, identifier, monkeypatch):
    monkeypatch.setattr("skills.registry.feral_home", Mock(side_effect=AssertionError("Must not access disk")))
    assert SkillRegistry().reload_skill_detail(identifier)[:2] == (False, "invalid_id")


@pytest.mark.parametrize("part", ["directory", "manifest.json", "impl.py"])
def test_redirected_selected_source_refused(home, part):
    directory = write_package(home, source=source())
    target = home / "target"
    if part == "directory":
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    else:
        path = directory / part
        path.rename(target)
        path.symlink_to(target)
    assert not SkillRegistry().reload_skill(SID)


def test_wrong_manifest_id_never_creates_foreign_registration(home):
    directory = write_package(home, source=source())
    path = directory / "manifest.json"
    data = json.loads(path.read_text())
    data["skill_id"] = "foreign_fixture"
    path.write_text(json.dumps(data))
    registry = SkillRegistry()
    assert registry.reload_skill_detail(SID)[:2] == (False, "identity_mismatch")
    assert registry.skills == {} and get_implementation("foreign_fixture") is None


def test_success_replaces_all_live_views_once_and_prunes_owned_alias(home):
    directory = write_package(home, source=source(1))
    registry = SkillRegistry()
    assert registry.reload_skill(SID)
    old = snapshot(registry)
    stamp = (directory / "impl.py").stat().st_mtime
    write_package(home, source=source(2), description="changed schema")
    # Same-size source and preserved mtime must not load stale pyc code.
    import os
    os.utime(directory / "impl.py", (stamp, stamp))
    candidate = registry.prepare_reload(SID)
    assert snapshot(registry) == old
    assert registry.publish_reload(candidate) == (True, "", "")
    new = snapshot(registry)
    assert new[0].description == "changed schema" and new[2] is not old[2]
    assert new[4] == old[4] + 1 and new[3] is candidate.module
    assert old[3].__name__ not in sys.modules
    assert registry.publish_reload(candidate)[0] is False
    # A repeated publish must not remove the already-live module alias.
    assert sys.modules[f"feral_skill_{SID}"] is new[3]
    assert new[3].__name__ in sys.modules
    assert asyncio.run(new[2].execute("read", {}, {}))["data"]["value"] == 2


@pytest.mark.parametrize("change", ["source", "manifest", "new_higher_priority", "implementation", "registration", "inplace_manifest"])
def test_stale_candidate_cannot_publish(home, change):
    location = "skills/generated" if change == "new_higher_priority" else "skills"
    directory = write_package(home, source=source(), location=location)
    registry = SkillRegistry()
    assert registry.reload_skill(SID)
    candidate = registry.prepare_reload(SID)
    if change == "source":
        (directory / "impl.py").write_text(source(9))
    elif change == "manifest":
        (directory / "manifest.json").write_text("invalid JSON")
    elif change == "new_higher_priority":
        write_package(home, source=source(8))
    elif change == "implementation":
        register_instance(SID, BaseSkill(SID))
    elif change == "registration":
        registry.register(SkillManifest(skill_id="unrelated_fixture", description="unrelated", brand={"name": "Other"}, endpoints=[]))
    else:
        registry.skills[SID].description = "edited live terms"
    observed = snapshot(registry)
    assert registry.publish_reload(candidate)[:2] == (False, "conflict")
    assert snapshot(registry) == observed and candidate.module.__name__ not in sys.modules


@pytest.mark.asyncio
async def test_explicit_http_replacement_removes_python_backing(home):
    write_package(home, source=source())
    registry = SkillRegistry()
    assert registry.reload_skill(SID)
    old = sys.modules[f"feral_skill_{SID}"]
    write_package(home, source=None, method="GET")
    assert await registry.reload_skill_detail_async(SID) == (True, "", "")
    assert registry.get_skill(SID) is None and get_implementation(SID) is None
    assert f"feral_skill_{SID}" not in sys.modules and old.__name__ not in sys.modules
    assert registry._reload_metadata[SID] == {"refresh_kind": "package", "implementation_ready": False}


def test_startup_and_lazy_paths_do_not_register_unusable_python_package(home):
    write_package(home, source="class Bare: pass")
    registry = SkillRegistry()
    registry._load_marketplace_skills()
    assert SID not in registry.skills and get_implementation(SID) is None
    manifest = SkillManifest.model_validate_json((home / "skills" / SID / "manifest.json").read_bytes())
    registry.register(manifest)
    generation = registry.generation
    assert registry.get_skill(SID) is None and registry.generation == generation
    # A failed lazy constructor/import is not automatically retried on lookup.
    (home / "skills" / SID / "impl.py").write_text(source())
    assert registry.get_skill(SID) is None
    assert registry.reload_skill(SID) and registry.get_skill(SID) is not None


def test_closed_capture_blocks_delayed_inherited_registration(home):
    with capture_registrations(SID):
        context = copy_context()
    with pytest.raises(ValueError, match="closed"):
        context.run(register_instance, SID, BaseSkill(SID))
    assert get_implementation(SID) is None


@pytest.mark.asyncio
async def test_actual_route_remains_responsive_cancel_does_not_publish_and_unrelated_write_survives(home, monkeypatch):
    from api.routes import skills as routes
    write_package(home, source=source())
    registry = SkillRegistry()
    assert registry.reload_skill(SID)
    before = snapshot(registry)
    bridge = ModuleType("feral_fixture_import_bridge")
    bridge.entered, bridge.release = threading.Event(), threading.Event()
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    write_package(home, source="import feral_fixture_import_bridge as bridge\n"
                  "bridge.entered.set()\nbridge.release.wait(5)\n" + source(2, decorate=True))
    monkeypatch.setattr(routes, "state", SimpleNamespace(skill_registry=registry))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        pending = asyncio.create_task(client.post("/api/skills/reload", params={"skill_id": SID}))
        try:
            assert await asyncio.to_thread(bridge.entered.wait, 2)
            listed = await asyncio.wait_for(client.get("/skills"), 1)
            assert listed.status_code == 200 and listed.json()[0]["description"] == "fixture"
            unrelated = BaseSkill("unrelated_fixture")
            register_instance("unrelated_fixture", unrelated)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert len(registry._reload_preparations) == 1
        finally:
            bridge.release.set()
        for _ in range(100):
            await asyncio.sleep(.01)
            staged = [module for name, module in sys.modules.items() if name.startswith("_feral_skill_staging_")
                      and str(home) in str(getattr(module, "__file__", "")) and module is not before[3]]
            if not staged:
                break
        assert not staged and snapshot(registry) == before
        assert get_implementation("unrelated_fixture") is unrelated
        assert not registry._reload_preparations


@pytest.mark.asyncio
async def test_actual_route_live_registry_replacement_refuses_orphan_publication(home, monkeypatch):
    from api.routes import skills as routes
    write_package(home, source=source())
    old_registry, new_registry = SkillRegistry(), SkillRegistry()
    assert old_registry.reload_skill(SID)
    before = snapshot(old_registry)
    bridge = ModuleType("feral_fixture_replacement_bridge")
    bridge.entered, bridge.release = threading.Event(), threading.Event()
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    write_package(home, source="import feral_fixture_replacement_bridge as bridge\n"
                  "bridge.entered.set()\nbridge.release.wait(5)\n" + source(2, decorate=True))
    owner = SimpleNamespace(skill_registry=old_registry)
    monkeypatch.setattr(routes, "state", owner)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        pending = asyncio.create_task(client.post("/api/skills/reload", params={"skill_id": SID}))
        try:
            assert await asyncio.to_thread(bridge.entered.wait, 2)
            owner.skill_registry = new_registry
        finally:
            bridge.release.set()
        response = await asyncio.wait_for(pending, 2)
    assert response.status_code == 409 and response.json()["code"] == "conflict"
    assert snapshot(old_registry) == before and new_registry.skills == {}
    assert new_registry._tool_cache == {} and new_registry.generation == 0
    assert not old_registry._reload_preparations
    assert len([name for name in sys.modules if name.startswith("_feral_skill_staging_")
                and str(home) in str(getattr(sys.modules[name], "__file__", ""))]) == 1


@pytest.mark.asyncio
async def test_cancelled_late_preparation_failure_logs_only_exception_class_and_releases_task(home, monkeypatch, caplog):
    write_package(home, source=source())
    registry = SkillRegistry()
    assert registry.reload_skill(SID)
    before = snapshot(registry)
    entered, release = threading.Event(), threading.Event()
    def failing_preparation(skill_id):
        entered.set()
        release.wait(5)
        raise RuntimeError("PRIVATE_PLUGIN_EXCEPTION_SENTINEL")
    monkeypatch.setattr(registry, "prepare_reload", failing_preparation)
    pending = asyncio.create_task(registry.reload_skill_detail_async(SID))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(registry._reload_preparations) == 1
    finally:
        release.set()
    for _ in range(100):
        await asyncio.sleep(.01)
        if not registry._reload_preparations:
            break
    assert not registry._reload_preparations and snapshot(registry) == before
    assert "RuntimeError" in caplog.text and "Cancelled reload preparation failed" in caplog.text
    assert "PRIVATE_PLUGIN_EXCEPTION_SENTINEL" not in caplog.text


@pytest.mark.asyncio
async def test_legacy_route_exception_response_and_log_are_redacted(home, monkeypatch, caplog):
    from api.routes import skills as routes
    class LegacyRegistry:
        def reload_skill_detail(self, skill_id):
            raise RuntimeError("PRIVATE_PLUGIN_EXCEPTION_SENTINEL")
    monkeypatch.setattr(routes, "state", SimpleNamespace(skill_registry=LegacyRegistry()))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/api/skills/reload", params={"skill_id": SID})
    assert response.status_code == 500 and response.json()["ok"] is False
    assert response.json()["code"] == "reload_raised" and response.json()["error"]
    assert "RuntimeError" in caplog.text
    assert "PRIVATE_PLUGIN_EXCEPTION_SENTINEL" not in response.text + caplog.text


@pytest.mark.asyncio
async def test_actual_route_discloses_shipped_inventory_and_python_package_readiness(home, monkeypatch):
    from api.routes import skills as routes
    registry = SkillRegistry()
    monkeypatch.setattr(routes, "state", SimpleNamespace(skill_registry=registry))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        shipped = await client.post("/api/skills/reload", params={"skill_id": "calendar_google"})
        assert shipped.status_code == 200 and shipped.json()["refresh_kind"] == "manifest_inventory"
        write_package(home, source=source())
        installed = await client.post("/api/skills/reload", params={"skill_id": SID})
        assert installed.json() == {"ok": True, "skill_id": SID, "refresh_kind": "package", "implementation_ready": True}


@pytest.mark.asyncio
async def test_actual_reviewed_executor_keeps_v1_after_failure_and_uses_v2_only_after_publication(home, monkeypatch):
    from agents.tool_runner import ToolRunner
    from api import state as state_module
    from security.exec_approvals import ApprovalManager
    from security.trust_ledger import TrustLedger
    from skills.executor import SkillExecutor
    from uuid import uuid4

    monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "on")
    write_package(home, source=source(1))
    # Real policy requires explicit review even though this handler is read-only.
    manifest_path = home / "skills" / SID / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["endpoints"][0].update(id="calculate", requires_user_approval=True, safety_tier="confirm")
    manifest_path.write_text(json.dumps(manifest))
    registry, executor = SkillRegistry(), SkillExecutor()
    assert registry.reload_skill(SID)
    async def send_text(session_id, text):
        pass
    orchestrator = SimpleNamespace(skills=registry, executor=executor, _mcp_client=None,
                                   daemons={}, _session_surfaces={}, _active_turns={}, _send_text=send_text)
    runner = ToolRunner(orchestrator, autonomy_mode="strict", trust_ledger=TrustLedger(persist=False),
                        approval_manager=ApprovalManager(db_path=str(home / "approvals.sqlite")))
    orchestrator.tool_runner = runner
    monkeypatch.setattr(state_module.state, "orchestrator", orchestrator)
    session = "reviewed-reload-fixture"

    async def invoke(approval=None, command=None):
        command = command or {"id": str(uuid4()), "name": SID + "__calculate", "args": {}}
        result = await runner.execute_tool_call_for_llm(session, command, registry.get_all_tools(), approval=approval)
        return result, command

    async def reviewed_value(expected):
        nonlocal session
        # Independent user task/session for each review, preserving anti-loop
        # enforcement instead of clearing its state or replaying one task.
        session = "reviewed-reload-fixture-" + str(uuid4())
        pending, command = await invoke()
        assert pending.get("status") == "pending_approval", pending
        assert runner.approve_pending(pending["request_id"], session_id="foreign", exact_once=True) is None
        grant = runner.approve_pending(pending["request_id"], session_id=session, exact_once=True)
        assert grant is not None
        result, _ = await invoke(grant["approval"], command)
        assert result["success"] and result["data"] == {"value": expected}
        reused, _ = await invoke(grant["approval"], command)
        assert reused["error_code"] == "invalid_approval"

    try:
        old = registry.get_skill(SID)
        await reviewed_value(1)
        assert old.calls == 1
        (home / "skills" / SID / "impl.py").write_text(source(2, decorate=True) + "\nraise RuntimeError('fail')")
        assert not (await registry.reload_skill_detail_async(SID))[0]
        assert registry.get_skill(SID) is old
        await reviewed_value(1)
        assert old.calls == 2
        (home / "skills" / SID / "impl.py").write_text(source(2))
        assert await registry.reload_skill_detail_async(SID) == (True, "", "")
        new = registry.get_skill(SID)
        assert new is not old and new.calls == 0
        session = "denied-reload-fixture-" + str(uuid4())
        pending, _ = await invoke()
        assert pending["status"] == "pending_approval"
        assert runner.deny_pending(pending["request_id"], session_id=session)["status"] == "PermissionOutcome::Deny"
        assert new.calls == 0 and old.calls == 2
        await reviewed_value(2)
        assert new.calls == 1 and old.calls == 2
    finally:
        await executor.close()
