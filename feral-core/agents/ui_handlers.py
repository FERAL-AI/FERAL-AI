from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional
from uuid import uuid4

from models.protocol import FeralMessage, SDUIPayload, SDUIPatchPayload

if TYPE_CHECKING:
    # Type-only: importing api.state at runtime here would close
    # an import cycle (api.state imports this module's callers).
    from api.state import BrainState

logger = logging.getLogger("feral.orchestrator")


async def handle_ui_event(
    orchestrator,
    session_id: str,
    action_id: str,
    event: str,
    value: Any = None,
    app_id: Optional[str] = None,
    screen_id: Optional[str] = None,
):
    """Dispatch a UI event to the right handler.

    When ``app_id`` is present, the event is scoped to a third-party
    GenUI app: we resolve the surface from ``screen_id`` (which has
    the shape ``<app_id>:<surface_id>:<session>`` assigned at
    ``AppRegistry.open_surface`` time), validate the action against
    the declared ``action_contract``, and route per the action's
    handler (navigate / patch / skill_call / app_event / close).
    Malformed app events are rejected with a short text reply so a
    compromised client can't invoke arbitrary skill endpoints by
    guessing action ids.
    """
    logger.info(
        "[%s] UI: %s -> %s = %r (app_id=%s)",
        session_id[:8], event, action_id, value, app_id or "",
    )

    if action_id.startswith(("confirm_", "reject_")):
        accepted = action_id.startswith("confirm_")
        confirmation_id = action_id[8:] if accepted else action_id[7:]
        pending_confirmations = getattr(orchestrator, "_pending_confirmations", None)
        if not isinstance(pending_confirmations, dict):
            return
        pending = pending_confirmations.get(confirmation_id)
        if not isinstance(pending, dict) or pending.get("session_id") != session_id:
            return
        # No await before consumption: a foreign/replayed response cannot
        # withdraw the legitimate owner or execute the action twice.
        pending_confirmations.pop(confirmation_id, None)
        app_action = pending.get("app_action")
        if app_action is not None or pending.get("confirmation_version") == 1:
            async def receipt(status: str) -> None:
                safe = app_action if isinstance(app_action, dict) else {}
                await orchestrator.send(session_id, FeralMessage(
                    session_id=session_id, hop="brain", type="confirmation_decision",
                    payload={"request_id": confirmation_id, "status": status,
                             **{key: safe.get(key, "") if isinstance(safe.get(key, ""), str) else ""
                                for key in ("app_id", "surface_id", "action_id", "screen_id")},
                             "scope": "app_action", "dispatch_accepted": status == "accepted",
                             "tool_outcome_verified": False},
                ))
            if pending.get("confirmation_version") == 1:
                now = time.time()
                created, expiry = pending.get("created_at"), pending.get("expires_at")
                import math
                if (not isinstance(created, (int, float)) or not isinstance(expiry, (int, float))
                        or type(created) not in (int, float) or type(expiry) not in (int, float)):
                    await receipt("error")
                    return
                try:
                    valid = (math.isfinite(created) and math.isfinite(expiry)
                             and created <= now and expiry > created and expiry - created <= 300)
                except OverflowError:
                    # isfinite converts built-in ints to float; an oversized
                    # timestamp must refuse consent rather than escape dispatch.
                    valid = False
                if not valid:
                    await receipt("error")
                    return
                if expiry <= now:
                    await receipt("expired")
                    return
                if not isinstance(pending.get("action_spec"), dict) or not isinstance(pending.get("manifest_snapshot"), dict):
                    await receipt("error")
                    return
            if not isinstance(app_action, dict) or any(not isinstance(app_action.get(key), str) or not app_action[key]
                                                       for key in ("app_id", "surface_id", "action_id", "screen_id")):
                await receipt("error")
                return
            if not accepted:
                await receipt("rejected")
                await orchestrator._send_text(session_id, "Cancelled. I won't run that action.")
                return
            try:
                dispatched = await _handle_app_action(
                    orchestrator, session_id=session_id, app_id=app_action["app_id"],
                    action_id=app_action["action_id"], event=app_action.get("event", "tap"),
                    value=app_action.get("value"), screen_id=app_action["screen_id"], _confirmed=True,
                    _confirmed_spec=pending.get("action_spec") if pending.get("confirmation_version") == 1 else None,
                    _confirmed_manifest=pending.get("manifest_snapshot") if pending.get("confirmation_version") == 1 else None,
                )
            except Exception:
                logger.exception("Confirmed app dispatch failed")
                dispatched = False
            await receipt("accepted" if dispatched is True else "error")
            return
        # Generic pre-existing tool confirmations retain their legacy shape.
        if accepted:
            await orchestrator._execute_tool_call(session_id, pending["tool_call"], pending.get("skills", []))
        else:
            await orchestrator._send_text(session_id, "Cancelled. I won't run that action.")
        return
    if action_id.startswith("perm_grant_"):
        await handle_permission_response(orchestrator, session_id, action_id[11:], granted=True, value=value)
        return
    if action_id.startswith("perm_deny_"):
        await handle_permission_response(orchestrator, session_id, action_id[10:], granted=False, value=value)
        return

    if app_id:
        await _handle_app_action(
            orchestrator,
            session_id=session_id,
            app_id=app_id,
            action_id=action_id,
            event=event,
            value=value,
            screen_id=screen_id,
        )
        return

    if action_id.startswith("call_"):
        tool_ref = action_id[5:]
        await orchestrator._execute_tool_call(session_id, {"name": tool_ref, "args": {}}, [])
        return

    await orchestrator.handle_command(
        session_id,
        f"The user interacted with '{action_id}' (event: {event}, value: {value}). What should happen next?",
    )


async def _handle_app_action(
    orchestrator,
    *,
    session_id: str,
    app_id: str,
    action_id: str,
    event: str,
    value: Any,
    screen_id: Optional[str],
    _confirmed: bool = False,
    _confirmed_spec: Optional[dict] = None,
    _confirmed_manifest: Optional[dict] = None,
) -> bool:
    """Dispatch a ui_event that belongs to a third-party GenUI app.

    Flow:
    1. Lookup AppRegistry on ``state`` — bail out with a polite text
       reply if the subsystem isn't initialised (boot race / tests).
    2. Resolve the surface_id from ``screen_id``. If the client didn't
       send an app-scoped screen_id we use the app's ``entry_surface_id``
       so the action still has a valid surface context.
    3. Validate the action against the surface's ``action_contract``.
       Unknown action ids are refused; the handler never falls through
       to ``handle_command`` on an app path.
    4. Dispatch per the action's declared handler.
    """
    _state: Optional[BrainState] = None
    try:
        from api.state import state as _brain_state
    except Exception:
        pass
    else:
        _state = _brain_state
    registry = getattr(_state, "app_registry", None) if _state else None
    if registry is None:
        await orchestrator._send_text(
            session_id,
            "The app registry isn't available right now. Please retry shortly.",
        )
        return False

    app = registry.get(app_id)
    if app is None:
        await orchestrator._send_text(
            session_id,
            f"App '{app_id}' is not installed on this brain.",
        )
        return False
    if _confirmed and _confirmed_manifest is not None and app.manifest.model_dump(mode="json") != _confirmed_manifest:
        return False

    surface_id = None
    if screen_id:
        resolved = registry.resolve_app_and_surface(screen_id)
        if resolved and resolved[0] == app_id:
            surface_id = resolved[1]
    if not surface_id:
        surface_id = app.manifest.entry_surface_id

    resolved_screen_id = (
        screen_id
        or registry.build_screen_id(
            app_id=app_id,
            surface_id=surface_id,
            scope=session_id,
        )
    )

    try:
        action_spec = registry.validate_action(
            app_id,
            surface_id,
            action_id,
            value=value,
        )
    except Exception as exc:
        logger.warning(
            "Rejecting unsigned app action: app=%s surface=%s action=%s (%s)",
            app_id, surface_id, action_id, exc,
        )
        await orchestrator._send_text(
            session_id,
            f"That action isn't in {app_id}'s surface contract.",
        )
        return False

    handler = action_spec.handler
    if _confirmed and _confirmed_spec is not None and action_spec.model_dump(mode="json") != _confirmed_spec:
        return False

    if action_spec.requires_confirmation and not _confirmed:
        canonical_screen = registry.build_screen_id(app_id=app_id, surface_id=surface_id, scope=session_id)
        if resolved_screen_id != canonical_screen:
            return False
        pending_confirmations = getattr(orchestrator, "_pending_confirmations", None)
        if not isinstance(pending_confirmations, dict):
            pending_confirmations = {}
            setattr(orchestrator, "_pending_confirmations", pending_confirmations)
        confirmation_id = str(uuid4())
        created_at = time.time()
        pending_confirmation = {
            "session_id": session_id,
            "app_action": {
                "app_id": app_id,
                "surface_id": surface_id,
                "action_id": action_id,
                "event": event,
                "value": value,
                "screen_id": resolved_screen_id,
            },
            "created_at": created_at,
            "expires_at": created_at + 300,
            "confirmation_version": 1,
            "action_spec": action_spec.model_dump(mode="json"),
            "manifest_snapshot": app.manifest.model_dump(mode="json"),
        }
        pending_confirmations[confirmation_id] = pending_confirmation
        confirm_root = {
            "type": "VStack",
            "spacing": 12,
            "padding": 18,
            "children": [
                {"type": "Text", "value": "Confirm action", "style": "headline"},
                {
                    "type": "Text",
                    "value": (
                        f"{app_id} requested '{action_id}'. "
                        "Confirm to continue."
                    ),
                    "style": "body",
                },
                {
                    "type": "HStack",
                    "spacing": 10,
                    "children": [
                        {
                            "type": "Button",
                            "action_id": f"confirm_{confirmation_id}",
                            "label": "Confirm",
                            "style": "primary",
                        },
                        {
                            "type": "Button",
                            "action_id": f"reject_{confirmation_id}",
                            "label": "Cancel",
                            "style": "secondary",
                        },
                    ],
                },
            ],
        }
        try:
            delivered = await _send_app_surface_payload(
                orchestrator,
                session_id=session_id,
                app_id=app_id,
                surface_id=surface_id,
                screen_id=resolved_screen_id,
                root=confirm_root,
                title=f"{app_id} confirmation",
                confirmation={"contract_version": 1, "request_id": confirmation_id, "session_id": session_id,
                              "app_id": app_id, "surface_id": surface_id, "action_id": action_id,
                              "screen_id": resolved_screen_id, "created_at": created_at, "expires_at": created_at + 300,
                              "scope": "app_action", "handler": action_spec.handler, "target": action_spec.target or "",
                              "event": event, "value": value, "requires_confirmation": True},
            )
        except Exception:
            delivered = False
        if delivered is not True:
            # A fast client may have answered, or another entry may have
            # replaced this ID, while delivery awaited. Withdraw only ours.
            if pending_confirmations.get(confirmation_id) is pending_confirmation:
                pending_confirmations.pop(confirmation_id, None)
            try:
                await orchestrator._send_text(
                    session_id,
                    "App confirmation delivery was not confirmed. Reconnect and request a fresh action review before retrying.",
                )
            except Exception:
                logger.debug("Undelivered app confirmation notice failed")
            return False
        await orchestrator._send_text(
            session_id,
            "Please confirm that app action before I execute it.",
        )
        return False

    if handler == "navigate":
        target = action_spec.target or ""
        if not target:
            await orchestrator._send_text(session_id, "App navigation had no target surface.")
            return False
        result = await registry.open_surface(
            app_id=app_id,
            surface_id=target,
            session_id=session_id,
            data=value if isinstance(value, dict) else {},
        )
        await _send_app_surface_payload(
            orchestrator,
            session_id=session_id,
            app_id=app_id,
            surface_id=target,
            screen_id=result["screen_id"],
            root=result["root"],
            title=f"{app_id} · {target}",
        )
        return True

    if handler == "close":
        await orchestrator._send_text(session_id, f"Closed {app_id}/{surface_id}.")
        return True

    if handler == "skill_call":
        if not action_spec.target:
            await orchestrator._send_text(
                session_id,
                f"App {app_id} declared a skill_call but no target endpoint.",
            )
            return False
        tool_call = {"name": action_spec.target, "args": value if isinstance(value, dict) else {}}
        try:
            await orchestrator._execute_tool_call(session_id, tool_call, [])
        except Exception as exc:
            logger.warning("App skill_call failed: %s", exc)
            await orchestrator._send_text(session_id, "The app's tool call failed. Private details are withheld.")
            return False
        return True

    if handler == "patch":
        patches = None
        if isinstance(value, dict):
            patches = value.get("patches")
        elif isinstance(value, list):
            patches = value
        if not isinstance(patches, list) or not patches:
            await orchestrator._send_text(
                session_id,
                "Patch action ignored because no patches payload was provided.",
            )
            return False
        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="sdui_patch",
                payload=SDUIPatchPayload(
                    screen_id=resolved_screen_id,
                    patches=patches,
                ).model_dump(),
            ),
        )
        return True

    # Default handler: "app_event" — the brain forwards the tuple to
    # the orchestrator as an LLM-visible event so the agent can decide
    # what to do next (e.g. log the action, surface a confirmation,
    # or call an unrelated skill). Keeps publishers productive even
    # before they wire a dedicated backend.
    await orchestrator.handle_command(
        session_id,
        (
            f"App '{app_id}' surface '{surface_id}' emitted action '{action_id}' "
            f"(event: {event}, value: {value}). What should happen next?"
        ),
    )
    return True


async def _send_app_surface_payload(
    orchestrator,
    *,
    session_id: str,
    app_id: str,
    surface_id: str,
    screen_id: str,
    root: dict,
    title: str,
    confirmation: Optional[dict] = None,
) -> bool:
    """Return an explicit delivery receipt, preserving best-effort node relay.

    Legacy node senders return None even when the node is missing. Such a
    return cannot authorize keeping an executable confirmation pending.
    """
    delivered = await orchestrator.send(
        session_id,
        FeralMessage(
            session_id=session_id,
            hop="brain",
            type="sdui",
            payload=SDUIPayload(
                screen_id=screen_id,
                root=root,
                confirmation=confirmation,
            ).model_dump(),
        ),
    )
    delivered = delivered is True

    _state: Optional[BrainState] = None
    try:
        from api.state import state as _brain_state
    except Exception:
        pass
    else:
        _state = _brain_state
    if _state is None:
        return delivered
    bindings = getattr(_state, "_daemon_session_bindings", {}) or {}
    node_id = None
    for bound_node, sessions in bindings.items():
        if session_id in sessions:
            node_id = bound_node
            break
    if not node_id:
        return delivered
    if not hasattr(_state, "send_to_daemon"):
        return delivered
    try:
        node_delivered = await _state.send_to_daemon(
            node_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="genui_push",
                payload={
                    "kind": "interactive",
                    "push_id": screen_id,
                    "app_id": app_id,
                    "surface_id": surface_id,
                    "screen_id": screen_id,
                    "title": title or f"{app_id}:{surface_id}",
                    "body": "",
                    "sdui": root,
                },
            ),
        )
        delivered = delivered or node_delivered is True
    except Exception as exc:
        logger.debug("genui_push relay failed: %s", exc)
    return delivered


async def send_permission_request(orchestrator, session_id: str, path: str, operation: str, reason: str = "") -> None:
    from uuid import uuid4 as _uuid4

    req_id = str(_uuid4())
    expires_at = time.time() + 300
    orchestrator._pending_permission_requests[req_id] = {
        "session_id": session_id,
        "path": path,
        "operation": operation,
        "expires_at": expires_at,
    }
    delivered = await orchestrator.send(
        session_id,
        FeralMessage(
            session_id=session_id,
            hop="brain",
            type="permission_request",
            payload={
                "request_id": req_id,
                "path": path,
                "operation": operation,
                "reason": reason or f"The agent needs {operation} access to {path}",
                "expires_at": expires_at,
                "scope": "persistent_workspace",
            },
        ),
    )
    # The entry has to be registered before the send, because a fast
    # client can answer before this coroutine resumes. But if the frame
    # reached nobody, an answer can never arrive, and leaving the entry
    # in place holds a grant open indefinitely for a question the
    # operator was never shown. Withdraw it and fail closed: an
    # unanswered permission request is a denied one.
    if delivered is False:
        orchestrator._pending_permission_requests.pop(req_id, None)
        logger.warning(
            "permission request for %s %s could not be delivered to session %s; "
            "treating as denied",
            operation, path, session_id,
        )


async def handle_permission_response(orchestrator, session_id: str, req_id: str, granted: bool, value=None) -> None:
    _ = value
    pending = orchestrator._pending_permission_requests.get(req_id)
    # Only the session that received the question can consume it. A foreign
    # response must not withdraw the legitimate owner's pending request.
    if not isinstance(pending, dict) or pending.get("session_id") != session_id:
        return
    orchestrator._pending_permission_requests.pop(req_id, None)

    async def receipt(status: str, **details) -> None:
        await orchestrator.send(session_id, FeralMessage(
            session_id=session_id, hop="brain", type="permission_decision",
            payload={"request_id": req_id, "status": status, **details},
        ))

    expires_at = pending.get("expires_at")
    if expires_at is not None and (not isinstance(expires_at, (int, float)) or expires_at <= time.time()):
        await receipt("expired")
        await orchestrator._send_text(session_id, "That folder-access request expired. Ask for a new request before granting access.")
        return
    path = pending["path"]
    operation = pending["operation"]
    if granted:
        from security.sandbox_policy import SandboxPolicy

        if operation not in {"read", "write", "readwrite"}:
            await receipt("error")
            await orchestrator._send_text(session_id, "The requested folder operation is unsupported. No access was granted.")
            return
        mode = "readwrite" if operation in {"write", "readwrite"} else "read"
        try:
            policy = SandboxPolicy.load_default()
            result = policy.grant_folder(path, mode=mode)
        except Exception:
            logger.exception("Folder grant failed for request %s", req_id)
            await receipt("error")
            await orchestrator._send_text(session_id, "The folder grant could not be confirmed. Check workspace grants before retrying.")
            return
        if not isinstance(result, dict) or result.get("ok") is not True or result.get("mode") != mode:
            await receipt("error")
            await orchestrator._send_text(session_id, "The security policy did not confirm this folder grant. Check workspace grants before retrying.")
            return
        await receipt("granted", path=result.get("path", path), mode=mode, scope="persistent_workspace")
        await orchestrator._send_text(session_id, f"Persistent workspace access granted to `{path}` ({mode}). This applies across this brain's sessions until revoked in Security.")
    else:
        await receipt("denied")
        await orchestrator._send_text(session_id, f"Access to `{path}` was denied. I won't access that path.")


async def handle_daemon_result(orchestrator, node_id: str, result: dict, session_id: str = None):
    request_id = result.get("request_id", "")
    success = result.get("success", False)
    data = result.get("data", {})
    output = data.get("output", "") if isinstance(data, dict) else str(data)
    error = data.get("error", "") if isinstance(data, dict) else ""
    if not output:
        output = result.get("stdout", "")
    if not error:
        error = result.get("stderr", result.get("error", ""))
    status = "success" if success else result.get("status", "error")
    logger.info(f"Daemon {node_id} -> {status}: {str(output)[:200]}")

    ack_payload = {
        "success": bool(success),
        "status_code": 200 if success else 500,
        "data": {"output": output} if output else (data if isinstance(data, dict) else None),
        "error": error or None,
    }
    try:
        orchestrator.tool_runner.resolve_daemon_ack(request_id, ack_payload)
    except Exception:
        pass

    daemon_session_map = orchestrator.tool_runner._daemon_session_map
    if not session_id:
        if request_id and request_id in daemon_session_map:
            session_id = daemon_session_map.pop(request_id)
        else:
            for req_id, sid in list(daemon_session_map.items()):
                session_id = sid
                del daemon_session_map[req_id]
                break

    if session_id:
        if status == "success":
            sdui = {
                "type": "VStack",
                "spacing": 12,
                "padding": 20,
                "children": [
                    {
                        "type": "HStack",
                        "spacing": 10,
                        "children": [
                            {"type": "Icon", "name": "checkmark.circle.fill", "size": 24, "color": "#00b894"},
                            {"type": "Text", "value": "Command Executed", "style": "headline", "color": "#00b894"},
                        ],
                    },
                    {"type": "Divider"},
                    {"type": "Text", "value": str(output)[:500] if output else "Done.", "style": "body"},
                ],
            }
        elif status == "denied":
            sdui = {
                "type": "VStack",
                "spacing": 12,
                "padding": 20,
                "children": [
                    {
                        "type": "HStack",
                        "spacing": 10,
                        "children": [
                            {"type": "Icon", "name": "xmark.shield.fill", "size": 24, "color": "#e17055"},
                            {"type": "Text", "value": "Command Denied", "style": "headline", "color": "#e17055"},
                        ],
                    },
                    {"type": "Divider"},
                    {
                        "type": "Text",
                        "value": error or "Blocked by security policy",
                        "style": "body",
                        "color": "#e17055",
                    },
                ],
            }
        else:
            sdui = {
                "type": "VStack",
                "spacing": 12,
                "padding": 20,
                "children": [
                    {
                        "type": "HStack",
                        "spacing": 10,
                        "children": [
                            {
                                "type": "Icon",
                                "name": "exclamationmark.triangle.fill",
                                "size": 24,
                                "color": "#fdcb6e",
                            },
                            {"type": "Text", "value": "Command Error", "style": "headline", "color": "#fdcb6e"},
                        ],
                    },
                    {"type": "Divider"},
                    {"type": "Text", "value": error or str(output) or "Unknown error", "style": "body"},
                ],
            }

        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="sdui",
                payload=SDUIPayload(root=sdui).model_dump(),
            ),
        )
