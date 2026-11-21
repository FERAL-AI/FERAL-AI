"""Authenticated operator-scoped reviewed hardware dispatch.

The existing HTTP security boundary has an operator API key and a trusted local
listener, not independent browser session credentials. Ownership is consequently
operator + server-selected primary SID; clients cannot claim a different owner.
Phone bearer, remote development bypass and proxy-reported loopback are refused.
The older hardware invoke route remains compatibility-only, outside this API.
"""
from dataclasses import asdict
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from api.keys import load_api_key
from api.state import state
from hardware.reviewed_dispatch import HardwareReviewError, _id, _uuid
from security.capability_grants import live_grants
from security.session_auth import is_localhost, transport_is_trusted

router = APIRouter(tags=["hardware-reviewed"])


def _operator(request: Request) -> str:
    host = request.client.host if request.client else None
    target = request.url
    origin = request.headers.get("origin")
    # Literal destination hosts prevent Host-header/DNS-rebinding local authority.
    local = transport_is_trusted(request.scope) and is_localhost(host) and is_localhost(target.hostname)
    if origin is not None:
        try:
            parsed = urlsplit(origin)
            same_origin = (parsed.scheme in {"http", "https"} and parsed.username is None
                           and parsed.password is None and parsed.hostname == target.hostname
                           and parsed.port == target.port and parsed.scheme == target.scheme
                           and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
        except ValueError:
            same_origin = False
        if not same_origin:
            raise HTTPException(403, detail="Untrusted request origin")
    if not local:
        try:
            key = load_api_key()
        except (OSError, ValueError):
            key = None
        auth = request.headers.get("authorization", "")
        if not key or not secrets.compare_digest(auth, "Bearer " + key):
            raise HTTPException(401, detail="Operator API key required")
    sid = getattr(state, "primary_session_id", None)
    try:
        return _id("operator:" + _id(sid))
    except (HardwareReviewError, TypeError):
        raise HTTPException(503, detail="Verified primary session unavailable") from None


def _mesh():
    if (getattr(state, "_native_bootstrap_required", False) is True
            or getattr(state, "memory", None) is None or getattr(state, "orchestrator", None) is None):
        raise HTTPException(503, detail="Agent initialization required before hardware actions")
    mesh = getattr(state, "hardware_mesh", None)
    if mesh is None:
        raise HTTPException(503, detail="Hardware mesh unavailable")
    mesh.reviewed_controller(policy=lambda: getattr(state, "policy", None), grants=live_grants)
    return mesh


def _failure(exc: HardwareReviewError):
    raise HTTPException(409, detail={"code": "hardware_review_refused", "message": str(exc)}) from None


@router.post("/api/hardware/reviewed/review")
async def review(body: dict, owner: str = Depends(_operator)):
    if set(body) - {"node_id", "command", "params", "timeout"} or not {"node_id", "command", "params"} <= set(body):
        raise HTTPException(400, detail="Exact node, command and parameters required; owner and confirmation flags are not accepted")
    mesh = _mesh()
    try:
        item = mesh._reviewed.review(owner=owner, node_id=body["node_id"], command=body["command"],
                                    params=body["params"], timeout=body.get("timeout", 10))
    except HardwareReviewError as exc:
        _failure(exc)
    return {"contract_version": 1, "review": asdict(item),
            "disclosure": "A node reports its own capabilities. Review does not verify safety or physical outcomes. Queued commands may take effect even if transport fails.",
            "separate_authorization_required": item.requires_confirmation}


@router.post("/api/hardware/reviewed/{review_id}/authorize")
async def authorize(review_id: str, body: dict, owner: str = Depends(_operator)):
    if body:
        raise HTTPException(400, detail="Authorization accepts an empty object only")
    mesh = _mesh()
    try:
        item = mesh._reviewed.get_review(owner=owner, review_id=review_id)
        token = mesh.authorize_reviewed(owner=owner, review_id=review_id)
    except HardwareReviewError as exc:
        _failure(exc)
    return {"contract_version": 1, "review_id": item.review_id, "owner": owner,
            "authorization_token": token, "expires_at": item.expires_at,
            "permission_tier": item.permission_tier,
            "operation": "authorize_exact_hardware_review", "dispatch_accepted": False,
            "physical_outcome_verified": False}


@router.post("/api/hardware/reviewed/{review_id}/dispatch")
async def dispatch(review_id: str, body: dict, owner: str = Depends(_operator)):
    if set(body) - {"authorization_token"}:
        raise HTTPException(400, detail="Only a separately issued authorization token is accepted")
    token = body.get("authorization_token")
    if token is not None:
        try:
            _uuid(token)
        except HardwareReviewError:
            raise HTTPException(400, detail="Invalid authorization token") from None
    mesh = _mesh()
    try:
        return {"contract_version": 1, "owner": owner,
                **mesh.dispatch_reviewed(owner=owner, review_id=review_id, authorization=token)}
    except HardwareReviewError as exc:
        _failure(exc)


@router.get("/api/hardware/reviewed/commands/{command_id}")
async def readback(command_id: str, owner: str = Depends(_operator)):
    mesh = _mesh()
    try:
        return {"contract_version": 1, "owner": owner, **mesh.read_reviewed(owner=owner, command_id=command_id)}
    except HardwareReviewError as exc:
        _failure(exc)
