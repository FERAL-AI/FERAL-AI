"""Actual runtime gates against a caller-started disposable SDK loopback host.

Input is a private JSON pipe containing the live manifest/service credential.
Only bounded non-secret verification results are printed. This is an embedding
contract check, not Brain startup or a production deployment installer.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from uuid import uuid4


async def exercise(home: Path, manifest: dict, credential: str) -> dict:
    os.environ.update(FERAL_HOME=str(home), FERAL_DATA_HOME=str(home / "data"),
                      FERAL_AUTONOMY="strict", FERAL_TOOL_CALL_CONTEXT="on")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "feral-core"))
    import httpx
    from fastapi import FastAPI
    from agents.tool_runner import ToolRunner
    from api import state as state_module
    from api.routes import skills as skill_routes
    from security.exec_approvals import ApprovalManager
    from security.sandbox_policy import SandboxPolicy
    from security.trust_ledger import TrustLedger
    from skills.executor import SkillExecutor
    from skills.marketplace import MarketplaceClient
    from skills.package import SkillPackage, SkillValidator, install_package
    from skills.registry import SkillRegistry
    from skills.impl import get_implementation

    source = home / "source"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(manifest))
    package = SkillPackage(source)
    assert package.load(), package.errors
    assert SkillValidator().validate(package) == []
    skill_id = package.manifest.skill_id
    assert skill_id == "sdk_node_math"
    assert not (home / "skills" / skill_id).exists()
    assert install_package(source, target_dir=home / "skills").valid
    registry = SkillRegistry()
    executor = SkillExecutor()
    executor.set_key(skill_id, credential)  # In-memory disposable service key, no OS vault write.
    notices = []

    async def send_text(session_id, text):
        notices.append((session_id, text))

    orch = SimpleNamespace(skills=registry, executor=executor, _mcp_client=None,
                           daemons={}, _session_surfaces={}, _active_turns={}, _send_text=send_text)
    runner = ToolRunner(orch, autonomy_mode="strict", trust_ledger=TrustLedger(persist=False),
                        approval_manager=ApprovalManager(db_path=str(home / "approvals.sqlite")))
    orch.tool_runner = runner
    state_module.state.skill_registry = registry
    state_module.state.orchestrator = orch
    # Explicit fixture policy, not a change to the developer's domain allowlist.
    policy = SandboxPolicy()
    policy._data["network"]["allowed_domains"] = ["127.0.0.1"]
    policy._data["network"]["blocked_domains"] = []
    state_module.state.policy = policy
    market = MarketplaceClient(skill_registry=registry)
    app = FastAPI()
    app.include_router(skill_routes.router)

    async def invoke(command, session="sdk-node-session", approval=None):
        return await runner.execute_tool_call_for_llm(session, command, registry.get_all_tools(), approval=approval)

    async def grant(command):
        pending = await invoke(command)
        assert pending["status"] == "pending_approval"
        assert pending["args"] == command["args"] and pending["session_id"] == "sdk-node-session"
        assert runner.approve_pending(pending["request_id"], session_id="foreign", exact_once=True) is None
        allowed = runner.approve_pending(pending["request_id"], session_id="sdk-node-session", exact_once=True)
        assert allowed is not None
        return allowed["approval"]

    def command(a=13, b=29):
        return {"id": str(uuid4()), "name": skill_id + "__calculate", "args": {"a": a, "b": b}}

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local-fixture") as client:
            reload_result = await client.post("/api/skills/reload", params={"skill_id": skill_id})
            assert reload_result.status_code == 200 and reload_result.json()["ok"] is True
        assert get_implementation(skill_id) is None  # HTTP backing is deliberately independent.
        pending = await invoke(command(7, 8), session="denied-node-session")
        assert pending["status"] == "pending_approval"
        assert runner.deny_pending(pending["request_id"], session_id="denied-node-session")["status"] == "PermissionOutcome::Deny"
        assert runner.approve_pending(pending["request_id"], session_id="denied-node-session", exact_once=True) is None

        blocked_command = command(1, 2)
        blocked_grant = await grant(blocked_command)
        policy._data["network"]["allowed_domains"] = ["example.invalid"]
        blocked = await invoke(blocked_command, approval=blocked_grant)
        assert blocked["success"] is False and blocked["status_code"] == 0, blocked
        assert "Blocked by sandbox policy" in blocked["error"], blocked
        policy._data["network"]["allowed_domains"] = ["127.0.0.1"]
        call = command()
        approval = await grant(call)
        result = await invoke(call, approval=approval)
        assert result["success"] is True and result["data"] == {"sum": 42}, result
        assert (await invoke(call, approval=approval))["error_code"] == "invalid_approval"
        stale = command(2, 3)
        stale_grant = await grant(stale)
        assert (await market.uninstall(skill_id))["success"] is True
        assert (await invoke(stale, approval=stale_grant))["error"] == "Skill not found: " + skill_id
        return {"contract": "sdk-node-runtime-v1", "validated": True, "installed": True,
                "registered_via_existing_route": True, "reviewed_sum": 42, "single_use_review": True,
                "denied": True, "foreign_review_refused": True, "domain_denied": True,
                "uninstalled": True, "stale_review_refused": True}
    finally:
        await executor.close()
        await market.close()


def main():
    if not __debug__:
        raise RuntimeError("Verification requires assertions, without Python -O")
    raw = sys.stdin.buffer.read(131073)
    if len(raw) > 131072:
        raise ValueError("Bounded fixture input exceeded")
    terms = json.loads(raw)
    with tempfile.TemporaryDirectory(prefix="feral-sdk-node-runtime-") as directory:
        result = asyncio.run(exercise(Path(directory), terms["manifest"], terms["credential"]))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
