"""Verify a bare SDK plugin is refused without executing a handler."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile


def exercise(home: Path) -> dict:
    os.environ.update(FERAL_HOME=str(home), FERAL_DATA_HOME=str(home / "data"))
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "feral-core"))
    sys.path.insert(0, str(repo / "sdk/python"))
    import httpx
    from fastapi import FastAPI
    from api import state as state_module
    from api.routes import skills as routes
    from skills.registry import SkillRegistry
    from skills.impl import get_implementation
    from feral_sdk import FeralPlugin, feral_tool

    class Bare(FeralPlugin):
        name = "sdk_bare_negative"

        @feral_tool()
        async def sum(self, a: int):
            raise AssertionError("Negative probe must not execute")

    package = home / "skills" / Bare.name
    package.mkdir(parents=True)
    (package / "manifest.json").write_text(json.dumps(Bare().to_manifest()))
    (package / "impl.py").write_text('from feral_sdk import FeralPlugin, feral_tool\n'
                                    'class Bare(FeralPlugin):\n'
                                    '    name = "sdk_bare_negative"\n'
                                    '    @feral_tool()\n'
                                    '    async def sum(self, a: int):\n'
                                    '        raise AssertionError("Must not execute")\n')
    registry = SkillRegistry()
    state_module.state.skill_registry = registry
    app = FastAPI()
    app.include_router(routes.router)

    async def reload():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            response = await client.post("/api/skills/reload", params={"skill_id": Bare.name})
            assert response.status_code == 409 and response.json()["ok"] is False
            assert response.json()["code"] == "invalid_implementation"
    asyncio.run(reload())
    assert Bare.name not in registry.skills and get_implementation(Bare.name) is None
    return {"reload_acknowledged": False, "backing_implementation_present": False,
            "handler_executed": False, "invalid_adapter_refused": True}


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Run verification without Python -O")
    with tempfile.TemporaryDirectory(prefix="feral-sdk-loader-negative-") as directory:
        print(json.dumps(exercise(Path(directory)), sort_keys=True))
