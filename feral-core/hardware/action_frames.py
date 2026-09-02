"""The one place a brain -> node ``hup_action_request`` frame is built.

Five call sites built this frame by hand -- ``hardware/mesh.py`` twice,
``hardware/protocol.py``, ``agents/tool_runner.py`` twice -- and a sixth
in ``gateway/protocol.py`` built a ``{"type": "command"}`` frame that has
not been a valid HUP type since 2026.7.0. All six got the same two things
wrong:

1. **No envelope.** HUP_SPEC.md section 5 requires ``hup_version`` and
   ``ts`` on every frame. These carried ``type`` and ``payload`` only.
   Nothing broke because no shipping SDK validates the envelope on
   inbound, but a third-party daemon written against the published spec
   is entitled to reject a frame with no version on it.
2. **No capability gate.** HUP_SPEC.md section 6 says the brain MUST NOT
   issue ``hup_action_request`` for a capability that is not in
   ``granted_capabilities``. Nothing consulted anything.

:func:`build_action_request` answers both, so a new sender gets them by
construction rather than by remembering.

The two refusals it can return are different in kind and the caller has
to be able to tell them apart, which is why this returns a result object
rather than raising or returning ``None``:

* ``denied`` -- the operator turned this capability off for this device.
  The caller reports it to whoever asked; it is not an error.
* ``frame`` -- send it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from models.protocol import hup_frame
from security.capability_grants import action_denied

logger = logging.getLogger("feral.hardware.action_frames")


@dataclass
class ActionRequest:
    """Either a frame to send, or the operator's reason for refusing it."""

    action_id: str
    name: str
    node_id: str
    frame: Optional[dict] = None
    denied_reason: str = ""
    #: Non-envelope extras the caller asked for, kept for tests.
    extras: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.frame is not None


def build_action_request(
    node_id: str,
    name: str,
    params: Optional[dict] = None,
    *,
    timeout_ms: int = 5000,
    action_id: str = "",
    grant_store=None,
    **extra,
) -> ActionRequest:
    """Build a spec-complete, capability-gated ``hup_action_request``.

    ``grant_store`` is injectable so a test can gate without standing up
    an ``AppState``; left ``None`` it resolves the running brain's store
    and fails open when there is not one (see
    ``security.capability_grants.action_denied`` for why that direction).
    """
    action_id = action_id or str(uuid4())[:8]
    name = str(name or "")

    reason = action_denied(node_id, name, store=grant_store)
    if reason:
        logger.info(
            "refusing hup_action_request name=%s node=%s: %s",
            name, node_id, reason,
        )
        return ActionRequest(
            action_id=action_id, name=name, node_id=node_id,
            denied_reason=reason, extras=dict(extra),
        )

    frame = hup_frame(
        "hup_action_request",
        {
            "action_id": action_id,
            "name": name,
            "params": params or {},
            "timeout_ms": int(timeout_ms),
        },
        **extra,
    )
    return ActionRequest(
        action_id=action_id, name=name, node_id=node_id,
        frame=frame, extras=dict(extra),
    )


__all__ = ["ActionRequest", "build_action_request"]
