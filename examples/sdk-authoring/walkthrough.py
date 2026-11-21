"""Verify an SDK-authored package through actual local runtime contracts."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from uuid import uuid4


async def exercise(home: Path) -> dict:
    # Set before runtime imports: do not read or mutate an installed profile.
    os.environ.update(FERAL_HOME=str(home), FERAL_DATA_HOME=str(home / "data"),
                      FERAL_AUTONOMY="strict", FERAL_TOOL_CALL_CONTEXT="on")
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "feral-core"))
    sys.path.insert(0, str(repo / "sdk" / "python"))

    import httpx
    from fastapi import FastAPI
    from agents.tool_runner import ToolRunner
    from api import state as state_module
    from api.routes import skills as skill_routes
    from security.exec_approvals import ApprovalManager
    from security.trust_ledger import TrustLedger
    from skills.executor import SkillExecutor
    from skills.impl import get_implementation
    from skills.marketplace import MarketplaceClient
    from skills.package import SkillPackage, SkillValidator, install_package, list_installed
    from skills.registry import SkillRegistry

    import importlib.util
    import shutil
    source = home / "author-package"
    shutil.copytree(Path(__file__).parent / "python_calculator", source)
    spec = importlib.util.spec_from_file_location("sdk_calculator_author", source / "impl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    author_plugin = module.SDKCalculator()
    (source / "manifest.json").write_text(json.dumps(author_plugin.to_manifest(), indent=2) + "\n")
    package = SkillPackage(source)
    assert package.load(), package.errors
    assert SkillValidator().validate(package) == []
    skill_id = package.manifest.skill_id
    target = home / "skills"
    assert not (target / skill_id).exists()  # install_package itself permits replacement.
    installed = install_package(source, target_dir=target)
    assert installed.valid and [p.manifest.skill_id for p in list_installed(target)] == [skill_id]

    registry = SkillRegistry()
    executor = SkillExecutor()
    notices = []

    async def send_text(session_id, text):
        notices.append({"session_id": session_id, "text": text})

    # Minimal embedding adapter, with real registry/executor/policy objects.
    # No model, scheduler, account, socket, runtime startup or mocked dispatch.
    orch = SimpleNamespace(skills=registry, executor=executor, _mcp_client=None,
                           daemons={}, _session_surfaces={}, _active_turns={},
                           _send_text=send_text)
    runner = ToolRunner(orch, autonomy_mode="strict", trust_ledger=TrustLedger(persist=False),
                        approval_manager=ApprovalManager(db_path=str(home / "approvals.sqlite")))
    orch.tool_runner = runner
    state_module.state.skill_registry = registry
    state_module.state.orchestrator = orch  # Existing executor defence-in-depth lookup.
    market = MarketplaceClient(skill_registry=registry)
    app = FastAPI()
    app.include_router(skill_routes.router)
    session = "developer-example-session"

    def call(args, name=None):
        return {"id": str(uuid4()), "name": name or skill_id + "__calculate", "args": args}

    async def invoke(command, session_id=session, approval=None):
        return await runner.execute_tool_call_for_llm(session_id, command, registry.get_all_tools(), approval=approval)

    async def review(command, session_id=session):
        pending = await invoke(command, session_id)
        assert pending["status"] == "pending_approval" and pending["session_id"] == session_id
        assert pending["tool_name"] == command["name"] and pending["args"] == command["args"]
        assert runner.approve_pending(pending["request_id"], session_id="foreign-session", exact_once=True) is None
        grant = runner.approve_pending(pending["request_id"], session_id=session_id, exact_once=True)
        assert grant is not None
        return grant["approval"]

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local-fixture") as client:
            response = await client.post("/api/skills/reload", params={"skill_id": skill_id})
            assert response.status_code == 200 and response.json()["ok"] is True, response.text
        assert registry.get_skill(skill_id) is get_implementation(skill_id)
        impl = registry.get_skill(skill_id)
        assert impl.plugin.invocations == 0
        command = call({"a": 13, "b": 29})
        denied_session = "denied-example-session"
        pending = await invoke(command, denied_session)
        assert pending["status"] == "pending_approval" and impl.plugin.invocations == 0
        assert runner.deny_pending(pending["request_id"], session_id="foreign-session") is None
        denied = runner.deny_pending(pending["request_id"], session_id=denied_session)
        assert denied["status"] == "PermissionOutcome::Deny"
        assert runner.approve_pending(pending["request_id"], session_id=denied_session, exact_once=True) is None
        assert impl.plugin.invocations == 0

        grant = await review(command)
        result = await invoke(command, approval=grant)
        assert result.get("success") is True and result.get("data") == {"sum": 42, "session_id": session}, result
        assert impl.plugin.invocations == 1
        reused = await invoke(command, approval=grant)
        assert reused["error_code"] == "invalid_approval" and impl.plugin.invocations == 1, reused

        invalid = call({"a": 13})
        invalid_grant = await review(invalid, "invalid-arguments-session")
        bad_args = await invoke(invalid, "invalid-arguments-session", invalid_grant)
        assert bad_args.get("success") is False and bad_args.get("error_code"), bad_args
        assert impl.plugin.invocations == 1

        bounded = call({"a": 1_000_001, "b": 1})
        bound_grant = await review(bounded, "bounded-arguments-session")
        bound_result = await invoke(bounded, "bounded-arguments-session", bound_grant)
        assert bound_result["success"] is False and bound_result["status_code"] == 500
        assert impl.plugin.invocations == 1

        # A queued reviewed action cannot revive an uninstalled extension.
        stale = call({"a": 2, "b": 3})
        stale_grant = await review(stale, "removed-package-session")
        removed = await market.uninstall(skill_id)
        assert removed == {"success": True, "skill_id": skill_id}
        assert not (target / skill_id).exists() and list_installed(target) == []
        assert registry.get_skill(skill_id) is None and skill_id not in registry.skills
        stale_result = await invoke(stale, "removed-package-session", stale_grant)
        assert stale_result.get("error") == "Skill not found: " + skill_id and impl.plugin.invocations == 1
        # Uninstall leaves an implementation object cached; registration still gates dispatch.
        assert get_implementation(skill_id) is impl
        return {
            "contract": "sdk-python-authoring-v1", "validated": True,
            "registered_via_existing_route": True, "denied_execution_count": 0,
            "reviewed_sum": result["data"]["sum"], "executions": impl.plugin.invocations,
            "single_use_review": True, "foreign_session_review_refused": True,
            "invalid_arguments_refused": True, "bounds_refused": True,
            "uninstalled": True, "stale_review_no_execution": True,
        }
    finally:
        await executor.close()
        await market.close()


def main():
    if not __debug__:
        raise RuntimeError("Run this verification walkthrough without Python -O")
    with tempfile.TemporaryDirectory(prefix="feral-sdk-authoring-") as folder:
        result = asyncio.run(exercise(Path(folder)))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
