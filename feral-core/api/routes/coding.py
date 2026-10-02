"""Consumer REST surface over the existing external-agent ACP skill."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Depends
from api.state import state
from bridges.coding_setup import read_setup, save_setup, validate_provider, prepare_provider, validate_workspace
from skills.impl import external_agent as engine

def _local_operator(request: Request):
    if (getattr(request.state, "phone_device_id", None)
            or getattr(request.state, "device_credential_kind", None)
            or request.client is None
            or request.client.host not in {"127.0.0.1", "::1", "localhost"}):
        raise HTTPException(403, "Coding workspace is available to the local brain operator only")


router = APIRouter(prefix="/api/coding", tags=["coding"], dependencies=[Depends(_local_operator)])


def _unpack(result):
    if not result.get("success"):
        raise HTTPException(result.get("status_code", 500), result.get("error", "Coding request failed"))
    return _details(result["data"])


def _details(data):
    for item in data.get("pending_permissions", []):
        session = engine._registry().find_by_permission(item["request_id"])
        if session:
            request = next((p for p in session.pending_permissions() if p.request_id == item["request_id"]), None)
            if request:
                item["details"] = request.raw.get("toolCall", {})
    return data


def _unpaused():
    if state.supervisor and state.supervisor.paused:
        raise HTTPException(423, "The agent is paused. Resume it before starting or approving coding work.")


def _reviewable_permission(request) -> bool:
    details = request.raw.get("toolCall", {})
    if not isinstance(details, dict):
        return False
    raw = details.get("rawInput", {})
    raw = raw if isinstance(raw, dict) else {}
    kind = details.get("kind")
    def text(key):
        return isinstance(raw.get(key), str) and bool(raw[key].strip())
    if kind == "execute":
        return text("command")
    if kind == "fetch":
        return text("url")
    if kind not in {"read", "edit", "delete"}:
        return False
    target = text("filePath") or text("path")
    content = details.get("content", [])
    if kind == "edit" and isinstance(content, list) and any(isinstance(item, dict) and item.get("type") == "diff"
                                       and isinstance(item.get("path"), str) and item["path"].strip()
                                       and isinstance(item.get("newText"), str) for item in content):
        return True
    locations = details.get("locations", [])
    target = target or (isinstance(locations, list) and any(
        isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].strip()
        for item in locations))
    return bool(target and (kind in {"read", "delete"} or text("diff") or isinstance(raw.get("content"), str)))


@router.get("")
async def overview():
    data = _unpack(await engine.ExternalAgentSkill().execute("list_agents", {}, {}))
    data.update(read_setup())
    data["paused"] = bool(state.supervisor and state.supervisor.paused)
    data["provider_note"] = "Coding uses its own explicit local Ollama model. Main chat credentials are not shared by this workspace."
    return data


@router.post("/provider")
async def configure_provider(body: dict):
    try:
        provider = validate_provider(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if any(s.turn_running for s in engine._registry().list()):
        raise HTTPException(409, "Stop active coding tasks before changing the model")
    if body.get("prepare"):
        try:
            provider = await prepare_provider(provider)
        except Exception as exc:
            raise HTTPException(424, "Could not prepare local coding model: " + str(exc)) from exc
    # An idle live agent retains its launch-time configuration; close it.
    for session in list(engine._registry().list()):
        await engine._registry().close(session.handle, forget=False)
    setup = read_setup()
    setup["provider"] = provider
    save_setup(setup)
    return {"provider": provider, "connection_verified": bool(provider.get("prepared"))}


@router.post("/workspaces")
async def grant_workspace(body: dict):
    raw = str(body.get("path") or "").strip()
    if not raw:
        raise HTTPException(400, "Choose a project folder")
    try:
        path = validate_workspace(Path(raw).expanduser())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    setup = read_setup()
    setup["workspaces"] = list(dict.fromkeys([*setup.get("workspaces", []), str(path)]))
    save_setup(setup)
    return {"path": str(path), "workspaces": setup["workspaces"]}


@router.post("/tasks")
async def start_task(body: dict):
    _unpaused()
    setup = read_setup()
    if not setup.get("provider"):
        raise HTTPException(424, "Configure a local Ollama model in Coding first")
    if not setup["provider"].get("prepared"):
        raise HTTPException(424, "Prepare the coding model in Coding first; the default Ollama context can truncate coding tools")
    handle = str(body.get("session_handle") or "")
    if handle:
        managed = engine._registry().get(handle)
        record = managed or engine._registry().index.get(handle)
        if record is None:
            raise HTTPException(404, "Coding session no longer exists")
        workspace = record.cwd
    else:
        workspace = str(Path(str(body.get("workspace_dir") or "")).expanduser().resolve())
    if workspace not in setup.get("workspaces", []):
        raise HTTPException(403, "Choose and grant this project folder before starting work")
    try:
        validate_workspace(workspace)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    args = {"prompt": body.get("prompt"), "workspace_dir": workspace,
            "session_handle": handle, "agent_id": "opencode", "wait_seconds": 1,
            "fresh_session": not bool(handle)}
    return _unpack(await engine.ExternalAgentSkill().execute("run_task", args, {}))


@router.get("/sessions/{handle}")
async def poll_session(handle: str):
    managed = engine._registry().get(handle)
    if managed is None:
        raise HTTPException(404, "Coding session is closed")
    status = await managed.wait_for_turn(1)
    return _details(await engine.ExternalAgentSkill()._finish_turn(managed, status))


@router.post("/permissions/{request_id}")
async def answer_permission(request_id: str, body: dict):
    decision = body.get("decision")
    if decision not in {"allow_once", "reject_once"}:
        raise HTTPException(400, "Choose allow_once or reject_once for this action")
    if decision == "allow_once":
        _unpaused()
        managed = engine._registry().find_by_permission(request_id)
        pending = next((p for p in managed.pending_permissions() if p.request_id == request_id), None) if managed else None
        if pending is None or not _reviewable_permission(pending):
            raise HTTPException(409, "The coding engine did not provide a concrete command or target. Deny the request and ask for a specific action.")
    return _unpack(await engine.ExternalAgentSkill().execute("respond_permission", {
        "request_id": request_id, "decision": decision, "wait_seconds": 1}, {}))


@router.post("/sessions/{handle}/cancel")
async def cancel_session(handle: str):
    return _unpack(await engine.ExternalAgentSkill().execute("close_session", {
        "session_handle": handle, "cancel_first": True}, {}))


@router.get("/activity")
async def activity():
    return _unpack(await engine.ExternalAgentSkill().execute("recall_activity", {"limit": 30}, {}))
