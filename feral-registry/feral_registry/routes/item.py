"""Item detail endpoint.

The path segment is an item **reference**: either the UUID primary key
or the item's ``name``. Names are what people and manifests actually
write (``feral install robot_ext``, ``skill_dependencies:
["robot_ext"]``), and ``UniqueConstraint("kind", "name", "version")``
makes a name a natural key, so the endpoint honours both. See
``feral_registry.resolve`` for the resolution order and for what a name
matching several rows does.

Public callers (no reviewer auth) only see items that are both
``approved`` and ``public``; everything else returns 404 to avoid
leaking the existence of pending or rejected submissions. That filter is
applied *inside* resolution, so a name cannot confirm a hidden row
either. Reviewers authenticated via the shared reviewer secret can fetch
any row.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Reviewer, optional_reviewer
from ..config import Settings, get_settings
from ..db import get_session
from ..resolve import AmbiguousReference, ItemNotFound, resolve_item
from ..schemas import ItemDetail, Kind

router = APIRouter()


@router.get("/item/{item_ref}", response_model=ItemDetail)
async def get_item(
    item_ref: str,
    kind: Kind | None = Query(
        default=None,
        description="Narrow a name lookup to one kind. Required when a name "
        "exists under more than one kind.",
    ),
    version: str | None = Query(
        default=None,
        description="Pin a name lookup to one version. Without it a name "
        "resolves to the highest version.",
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    reviewer: Reviewer | None = Depends(optional_reviewer),
) -> ItemDetail:
    try:
        item, publisher, _resolved_by = await resolve_item(
            session,
            item_ref,
            kind=kind,
            version=version,
            include_hidden=reviewer is not None,
        )
    except ItemNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found") from None
    except AmbiguousReference as exc:
        # 409, not 404 and not an arbitrary pick: the item exists, the
        # request just does not say which one, and the message names the
        # options so the caller can.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return ItemDetail(
        id=item.id,
        kind=item.kind,  # type: ignore[arg-type]
        name=item.name,
        version=item.version,
        manifest=json.loads(item.manifest_json),
        publisher=publisher.github_login,
        publisher_pubkey=publisher.pubkey_hex,
        sha256=item.sha256,
        size_bytes=item.size_bytes,
        signature_b64=item.signature_b64,
        download_url=f"{settings.public_base_url}/api/v1/blobs/{item.sha256}",
        downloads=item.downloads,
        verified=item.verified,
        created_at=item.created_at,
        status=item.status,  # type: ignore[arg-type]
        visibility=item.visibility,  # type: ignore[arg-type]
    )
