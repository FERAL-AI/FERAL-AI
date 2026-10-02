from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from api.routes import coding


@pytest.mark.parametrize("details,expected", [
    ({"kind": "read", "rawInput": {}, "locations": []}, False),
    ({"kind": "execute", "rawInput": {"command": " "}}, False),
    ({"kind": "execute", "rawInput": {"command": "python check.py"}}, True),
    ({"kind": "read", "locations": [{"path": "hello.txt"}]}, True),
    ({"kind": "edit", "content": [{"type": "diff", "path": "hello.txt", "newText": ""}]}, True),
    ({"kind": "execute", "rawInput": {"filePath": "hello.txt"}}, False),
    ({"kind": "read", "rawInput": {"command": "python check.py"}}, False),
    ({"kind": "edit", "rawInput": {"filePath": "hello.txt"}}, False),
    ({"rawInput": [], "content": None, "locations": None}, False),
])
def test_review_requires_concrete_command_or_target(details, expected):
    assert bool(coding._reviewable_permission(SimpleNamespace(raw={"toolCall": details}))) is expected


@pytest.mark.asyncio
async def test_unknown_approval_never_reaches_engine(monkeypatch):
    request = SimpleNamespace(request_id="missing-target", raw={"toolCall": {"kind": "read", "rawInput": {}}})
    managed = SimpleNamespace(
        handle="review-fixture", conversation_id="", pending_permissions=lambda: [request]
    )
    registry = SimpleNamespace(
        find_by_permission=lambda _: managed,
        get=lambda handle: managed if handle == managed.handle else None,
        index=SimpleNamespace(get=lambda _: None),
    )
    monkeypatch.setattr(coding.engine, "_registry", lambda: registry)
    monkeypatch.setattr(coding, "_unpaused", lambda: None)
    execute = AsyncMock()
    monkeypatch.setattr(coding.engine.ExternalAgentSkill, "execute", execute)
    with pytest.raises(HTTPException) as error:
        await coding.answer_permission("missing-target", {"decision": "allow_once"})
    assert error.value.status_code == 409
    execute.assert_not_awaited()
