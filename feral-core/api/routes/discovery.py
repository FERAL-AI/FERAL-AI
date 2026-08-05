"""Phase 13 (audit-r10 overhaul) — brain identity discovery endpoint.

Lightweight identity endpoint the iOS onboarding wizard hits after
finding the brain via mDNS to confirm "yes, this is a real FERAL
brain on this LAN".

Wire shape::

    GET /api/discovery/brain
    {
      "brain_id": "<stable per-install id>",
      "host": "<hostname>",
      "port": <int>,
      "version": "<feral version string>",
      "fingerprint": "<stable hash derived from primary_session_id>"
    }

Read-only, LAN-public — same posture as ``/api/capabilities``.
"""
from __future__ import annotations

import hashlib
import logging
import socket

from fastapi import APIRouter, Request

logger = logging.getLogger("feral.api.discovery")

router = APIRouter(tags=["discovery"])


@router.get("/api/discovery/brain")
async def get_brain_identity(request: Request):
    # ``app.state.feral`` is the injection point the tests use, and it
    # is now actually set at startup in api/server.py. Before that it was
    # never assigned anywhere in production, so this raised
    # AttributeError and the route 500'd on every real call while the
    # tests passed, because they set it themselves. That is why nothing
    # caught it: the test wired a seam production did not.
    #
    # The route is on the phone-bearer GET allowlist and is used by
    # onboarding and discovery, so a hard 500 reads to an operator as
    # flaky pairing rather than as a broken endpoint.
    state = request.app.state.feral
    brain_id = getattr(state, "brain_id", "") or ""
    primary_sid = getattr(state, "primary_session_id", "") or ""
    fingerprint = hashlib.sha256(primary_sid.encode()).hexdigest()[:16] if primary_sid else ""

    version = "unknown"
    try:
        from version import VERSION as version
    except Exception:
        pass

    host = socket.gethostname()
    port_val = 9090
    try:
        from config import brain_port
        port_val = brain_port()
    except Exception:
        pass

    return {
        "brain_id": brain_id,
        "host": host,
        "port": port_val,
        "version": version,
        "fingerprint": fingerprint,
    }
