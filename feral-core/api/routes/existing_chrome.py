"""Native local Chrome connection review; no page action endpoint."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from api.browser_view import BrowserViewError
from api.existing_chrome_connection import ChromeConnectionManager, local_owner_scope
from api.state import state
from memory.runtime_session_checkpoint import CheckpointValidationError, validate_uuid
from skills.impl.existing_chrome import ExistingChromeError

router = APIRouter()
chrome_connections = ChromeConnectionManager(lambda: state)


class ChromeOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: StrictStr = Field(min_length=1, max_length=1024)


class ChromeConnect(ChromeOwner):
    consent: StrictBool


class ChromeConnection(ChromeOwner):
    connection_id: StrictStr = Field(min_length=36, max_length=36)


class ChromeSelect(ChromeConnection):
    target_id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


async def response(request: Request, body: ChromeOwner, operation):
    from api.routes.marketplace_browser import _browser_view_local
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    try:
        _browser_view_local(request)
        if isinstance(body, ChromeConnection):
            validate_uuid(body.connection_id)
        with local_owner_scope(body.session_id):
            value = await operation()
        return JSONResponse(content=value, headers=headers)
    except CheckpointValidationError:
        code, status = "existing_chrome_identity_invalid", 422
    except (ExistingChromeError, BrowserViewError) as exc:
        code, status = exc.code, exc.status
    except Exception:
        code, status = "existing_chrome_operation_unconfirmed", 502
    return JSONResponse(status_code=status, headers=headers,
                        content={"success": False, "error_code": code,
                                 "error": "Chrome connection was not confirmed. Review this thread and browser before retrying."})


@router.post("/api/browser/existing/status")
async def existing_status(request: Request, body: ChromeOwner):
    return await response(request, body, lambda: chrome_connections.status(body.session_id))


@router.post("/api/browser/existing/connect")
async def existing_connect(request: Request, body: ChromeConnect):
    return await response(request, body, lambda: chrome_connections.connect(body.session_id, body.consent))


@router.post("/api/browser/existing/select")
async def existing_select(request: Request, body: ChromeSelect):
    return await response(request, body, lambda: chrome_connections.select(body.session_id, body.connection_id, body.target_id))


@router.post("/api/browser/existing/disconnect")
async def existing_disconnect(request: Request, body: ChromeConnection):
    return await response(request, body, lambda: chrome_connections.disconnect(body.session_id, body.connection_id))


router.add_event_handler("shutdown", chrome_connections.shutdown)
