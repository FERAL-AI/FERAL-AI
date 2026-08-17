"""Publish endpoint: accept signed bundle tarballs from authenticated publishers."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_publisher
from ..bundle import validate_bundle_for_kind
from ..config import Settings, get_settings
from ..db import get_session
from ..models import (
    ITEM_STATUS_SUBMITTED,
    ITEM_VISIBILITY_PRIVATE,
    Item,
    Publisher,
    ReviewEvent,
)
from ..schemas import Manifest, PublishResponse, validate_manifest_for_kind
from ..signing import sha256_bytes, verify_bundle_signature

router = APIRouter()

MAX_BUNDLE_BYTES = 50 * 1024 * 1024  # 50 MB


async def _write_blob(blob_dir: Path, sha256: str, data: bytes) -> Path:
    path = blob_dir / f"{sha256}.tar.gz"
    await anyio.to_thread.run_sync(path.write_bytes, data)
    return path


@router.post("/publish", response_model=PublishResponse)
async def publish(
    bundle: UploadFile = File(...),
    signature: str = Form(...),
    manifest_json: str = Form(...),
    publisher: Publisher = Depends(current_publisher),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PublishResponse:
    if publisher.pubkey_hex is None:
        raise HTTPException(
            status.HTTP_412_PRECONDITION_FAILED,
            "register pubkey via POST /auth/github/register_pubkey",
        )

    try:
        manifest_obj = Manifest.model_validate_json(manifest_json)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid manifest: {exc}") from exc

    missing = validate_manifest_for_kind(manifest_obj)
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"manifest kind={manifest_obj.kind} is missing required key(s): {', '.join(missing)}",
        )

    data = await bundle.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty bundle")
    if len(data) > MAX_BUNDLE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "bundle too large")

    sha = sha256_bytes(data)
    if not verify_bundle_signature(publisher.pubkey_hex, signature, sha):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "signature verification failed")

    # The metadata manifest above describes the bundle; this reads the
    # bundle. Both have to hold, because the metadata is what the catalog
    # shows and the bundle's own manifest.json is what FERAL loads. See
    # feral_registry.bundle for why this runs after the signature check
    # (never inspect bytes whose publisher has not been established) and
    # for what it deliberately does not check.
    bundle_problems = validate_bundle_for_kind(manifest_obj.kind, data)
    if bundle_problems:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"bundle would not install: {'; '.join(bundle_problems)}",
        )

    existing = await session.execute(
        select(Item).where(
            Item.kind == manifest_obj.kind,
            Item.name == manifest_obj.name,
            Item.version == manifest_obj.version,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "item with same kind+name+version exists")

    blob_path = await _write_blob(settings.blob_dir, sha, data)
    verified = publisher.github_login.lower() in settings.featured_publishers

    item = Item(
        kind=manifest_obj.kind,
        name=manifest_obj.name,
        version=manifest_obj.version,
        author_id=publisher.id,
        manifest_json=json.dumps(manifest_obj.model_dump(), sort_keys=True),
        sha256=sha,
        blob_path=str(blob_path),
        size_bytes=len(data),
        signature_b64=signature,
        verified=verified,
        status=ITEM_STATUS_SUBMITTED,
        visibility=ITEM_VISIBILITY_PRIVATE,
    )
    session.add(item)
    await session.flush()  # populate item.id without ending the txn
    session.add(
        ReviewEvent(
            item_id=item.id,
            event="publish_received",
            actor=f"publisher:{publisher.github_login}",
            notes=None,
        )
    )
    await session.commit()
    await session.refresh(item)

    return PublishResponse(
        id=item.id,
        sha256=sha,
        download_url=f"{settings.public_base_url}/api/v1/blobs/{sha}",
        verified=verified,
        status=item.status,  # type: ignore[arg-type]
        visibility=item.visibility,  # type: ignore[arg-type]
    )
