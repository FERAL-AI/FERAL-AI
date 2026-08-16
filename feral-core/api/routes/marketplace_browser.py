"""Marketplace and browser control HTTP endpoints."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.state import state

router = APIRouter()

# Kinds the registry publishes and this route can install. `app` is not
# here: third-party GenUI apps install through AppRegistry via
# /api/apps/install, which runs its own signature verification.
INSTALLABLE_KINDS = ("skill", "daemon", "mcp", "channel", "provider", "memory", "workflow", "agent")


def _with_permissions(item: dict) -> dict:
    """Attach rendered permission copy to a catalog row that declares one.

    The public catalog endpoint returns one row per item and does not
    carry the manifest, so most rows have no permission list to show.
    Where one is present (a row that carries ``permissions``, or an
    embedded manifest that does) it is rendered here so the card can
    show it before the user commits to a download.

    A catalog row is unsigned metadata, so this is a hint, not the
    decision. The list the user consents to is the one
    ``/api/marketplace/preview`` reads out of the verified bundle, and
    that is the only one the install is bound to.
    """
    from models.skill_manifest import describe_permissions

    if not isinstance(item, dict):
        return item
    perms = item.get("permissions")
    if perms is None:
        manifest = item.get("manifest")
        if isinstance(manifest, dict):
            perms = manifest.get("permissions")
    if not perms:
        return item
    perms = [str(p) for p in perms]
    return {**item, "permissions": perms, "permission_details": describe_permissions(perms)}


def _refused(error: str, status: int = 400, **extra):
    """A refusal the client can render. Never 200: the v2 client treats a
    2xx as "installed", and a consent failure is not an install."""
    return JSONResponse(status_code=status, content={"success": False, "error": error, **extra})


# ─────────────────────────────────────────────
# Marketplace API
# ─────────────────────────────────────────────


@router.get("/api/marketplace/search")
async def marketplace_search(q: str = ""):
    """Search the skill marketplace."""
    if not state.marketplace:
        return {"results": []}
    results = await state.marketplace.search(q)
    return {"results": [_with_permissions(r) for r in results]}


@router.get("/api/marketplace/catalog")
async def marketplace_catalog(kind: str = "skill", q: str = "", sort: str = "newest"):
    """Browse the remote registry catalog, partitioned by kind.

    kind ∈ {"skill", "daemon", "mcp"}. Proxies to the configured
    ``FERAL_REGISTRY_URL`` (default https://registry.feral.sh) so the
    Settings UI has a single, stable endpoint across install targets.

    Walks the fallback URL list (see ``cli.publish.registry_base_urls``)
    on connection / DNS failures so a user whose network can't resolve
    the primary host (e.g. IPv6-only AAAA records) still reaches the
    registry via the direct Fly URL.
    """
    import httpx

    from cli.publish import registry_base_urls

    bases = registry_base_urls()
    last_error: Exception | None = None
    last_status: int | None = None
    for base in bases:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{base}/api/v1/catalog",
                    params={"kind": kind, "q": q, "sort": sort},
                )
                if resp.status_code != 200:
                    last_status = resp.status_code
                    continue
                data = resp.json()
                items = data.get("items") or data.get("results") or []
                return {
                    "items": [_with_permissions(it) for it in items],
                    "kind": kind,
                    "source": base,
                    "tried": bases,
                }
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        return {
            "items": [],
            "error": f"registry unreachable: {last_error}",
            "tried": bases,
        }
    return {
        "items": [],
        "error": f"registry returned {last_status}",
        "tried": bases,
    }


@router.post("/api/marketplace/preview")
async def marketplace_preview(body: dict):
    """Step one of installing: say what the package is and what it reaches.

    Downloads the bundle, verifies its SHA-256 and the publisher's
    detached Ed25519 signature through the same code ``feral install``
    runs, reads the manifest out of the *verified* archive, and returns
    the permission list, the signature status and a single-use install
    token. Nothing is written to ``~/.feral`` here.

    A package that does not verify gets no token, so there is no way to
    reach :func:`marketplace_install` for it. The permission list is only
    ever shown for bytes that verified: showing it for anything else
    would be decoration on an unauthenticated payload.
    """
    if not state.marketplace:
        return _refused("Marketplace not available", status=503)

    kind = (body.get("kind") or "skill").lower()
    item_id = body.get("id") or body.get("skill_id")
    if not item_id:
        return _refused("preview requires an item id")
    if kind not in INSTALLABLE_KINDS:
        return _refused(f"unknown kind '{kind}'")

    try:
        result = await state.marketplace.preview_from_registry(kind, item_id)
    except Exception as exc:
        return _refused(f"preview failed: {exc}")
    if not result.get("success"):
        return _refused(result.get("error") or "preview failed", signature=result.get("signature"))
    return result


@router.post("/api/marketplace/install")
async def marketplace_install(body: dict):
    """Install an item the user has previewed and approved.

    Requires ``{kind, id, install_token}``. The token comes from
    ``POST /api/marketplace/preview`` and is bound to that package's
    verified bytes, so the thing that gets installed is the thing whose
    permissions were on screen.

    There is no unverified fallback. This route used to probe for
    ``install_from_registry``, find nothing (the method existed on
    AppRegistry, not on MarketplaceClient), swallow the AttributeError
    and quietly install over the unsigned path instead, under a page
    header that reads "Signed community registry". The probe and the
    fallthrough are both gone.

    ``source_url`` is not accepted here. It is the developer's
    local-iteration path and it performs no verification, so it stays on
    the CLI where the person typing it is the person who wrote the code.
    """
    if not state.marketplace:
        return _refused("Marketplace not available", status=503)

    kind = (body.get("kind") or "skill").lower()
    item_id = body.get("id") or body.get("skill_id")
    install_token = body.get("install_token") or ""

    if body.get("source_url"):
        return _refused(
            "source_url installs are unverified and are not available over HTTP. "
            "Use `feral install <id>` for a published item, or the CLI for a local build.",
            status=403,
        )
    if not item_id:
        return _refused("install requires an item id")
    if kind not in INSTALLABLE_KINDS:
        return _refused(f"unknown kind '{kind}'")
    if not install_token:
        return _refused(
            "install requires an install_token from POST /api/marketplace/preview, "
            "so the permissions and signature are shown before anything is installed.",
            status=403,
            code="preview_required",
        )

    try:
        result = await state.marketplace.install_from_registry(kind, item_id, install_token=install_token)
    except Exception as exc:
        return _refused(f"registry install failed: {exc}")
    if not result.get("success"):
        status = 403 if result.get("code") == "preview_required" else 400
        return _refused(result.get("error") or "install failed", status=status, code=result.get("code"))
    return result


@router.get("/api/marketplace/installed")
async def marketplace_installed():
    """List all marketplace-installed skills."""
    if not state.marketplace:
        return {"skills": []}
    return {"skills": state.marketplace.list_installed()}


@router.delete("/api/marketplace/uninstall/{skill_id}")
async def marketplace_uninstall(skill_id: str):
    """Uninstall a marketplace skill."""
    if not state.marketplace:
        return {"success": False, "error": "Marketplace not available"}
    return await state.marketplace.uninstall(skill_id)


@router.post("/api/marketplace/update/{skill_id}")
async def marketplace_update(skill_id: str):
    """Update a marketplace skill to latest version."""
    if not state.marketplace:
        return {"success": False, "error": "Marketplace not available"}
    return await state.marketplace.update(skill_id)


# ─────────────────────────────────────────────
# Browser Control API
# ─────────────────────────────────────────────


@router.post("/api/browser/init")
async def browser_init():
    """Initialize browser control (CDP connection)."""
    if not state.browser:
        return {"error": "Browser controller not available"}
    ok = await state.browser.initialize()
    return {"connected": ok}


@router.post("/api/browser/navigate")
async def browser_navigate(body: dict):
    if not state.browser or not state.browser.connected:
        return {"error": "Browser not connected"}
    return await state.browser.navigate(body.get("url", ""))


@router.post("/api/browser/screenshot")
async def browser_screenshot(body: dict):
    if not state.browser or not state.browser.connected:
        return {"error": "Browser not connected"}
    return await state.browser.screenshot(body.get("full_page", False))


@router.post("/api/browser/snapshot")
async def browser_snapshot():
    if not state.browser or not state.browser.connected:
        return {"error": "Browser not connected"}
    return await state.browser.snapshot()


@router.post("/api/browser/action")
async def browser_action(body: dict):
    """Execute a browser action (click, type, scroll, etc.)."""
    if not state.browser or not state.browser.connected:
        return {"error": "Browser not connected"}
    action = body.get("action", "")
    if action == "click":
        return await state.browser.click(body.get("selector", body.get("ref_or_selector", "")))
    elif action == "hover":
        return await state.browser.hover(body.get("selector", body.get("ref_or_selector", "")))
    elif action in ("type", "type_text"):
        return await state.browser.type_text(
            body.get("selector", body.get("ref_or_selector", "")),
            body.get("text", ""),
        )
    elif action == "fill_form":
        return await state.browser.fill_form(body.get("fields", {}))
    elif action == "scroll":
        return await state.browser.scroll(body.get("direction", "down"), body.get("amount", 500))
    elif action == "evaluate":
        return await state.browser.evaluate(body.get("js_code", ""))
    elif action == "select":
        return await state.browser.select(body.get("selector", ""), body.get("value", ""))
    elif action == "console_logs":
        return await state.browser.get_console_logs(body.get("limit", 50), body.get("clear", False))
    elif action == "pdf":
        return await state.browser.get_page_pdf(
            body.get("print_background", True),
            body.get("landscape", False),
        )
    elif action == "wait":
        return await state.browser.wait(body.get("ms", 1000))
    return {"error": f"Unknown action: {action}"}
