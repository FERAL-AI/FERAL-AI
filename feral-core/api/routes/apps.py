"""REST routes for third-party GenUI apps (see agents/app_registry.py).

Every endpoint is shallow — the real logic lives in AppRegistry +
HybridGenerator. The routes are intentionally small so both a v2
client and an external `feral app` CLI talk to the same surface.

Routes
------
GET    /api/apps                              — list installed apps
GET    /api/apps/{app_id}/manifest            — fetch the stored manifest
POST   /api/apps/preview                      — verify + describe an install, mint an install_token
POST   /api/apps/install                      — spend an install_token
DELETE /api/apps/{app_id}                     — uninstall
POST   /api/apps/{app_id}/open                — render a surface + push sdui
POST   /api/apps/{app_id}/surfaces/{id}/render — render without pushing
POST   /api/apps/{app_id}/dispatch            — validate + execute an action (REST parity with ui_event)

Installing an app is two steps
------------------------------
An app is a bundle of SDUI surfaces rendered in a sandboxed iframe, so
its own blast radius is data reach, not in-process code. Its
``skill_dependencies`` are a different matter: a skill executes Python in
this process at load time (``skills/registry.py`` ``_try_load_dynamic_impl``
calls ``spec.loader.exec_module``). Installing an app therefore installs
code, and until now it did so with no signature check and nothing on
screen.

``POST /api/apps/preview`` verifies the bundle, resolves every declared
skill dependency, and returns what the app itself reaches plus the
*named* skills it will pull in with their permissions. It mints a
single-use ``install_token`` bound to all of that.
``POST /api/apps/install`` spends the token. Without one it refuses.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agents.app_registry import (
    AppRegistryError,
    RegistryStage,
    UnapprovedRegistryItemError,
    UnverifiedManifestError,
    describe_app_permissions,
    manifest_digest,
    skill_dependency_impact,
)
from api.state import state
from genui.permissions_policy import PolicyViolation, enforce_install_policy
from skills.marketplace import read_consent_token, sign_consent_token

logger = logging.getLogger("feral.api.apps")

router = APIRouter(tags=["apps"])

# Wall-clock budget for `git clone` in the preview path. Named so the
# timeout-and-kill behaviour is testable without a 120s test.
GIT_CLONE_TIMEOUT_S = 120

# How long a previewed app install stays installable, and how many may be
# outstanding. Matches the marketplace's window: the point of the token is
# that the thing described is the thing installed, and a stale description
# is not consent.
APP_PREVIEW_TTL_SECONDS = 300
MAX_PENDING_APP_PREVIEWS = 8


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Kill a child that outran its timeout and reap it.

    asyncio.wait_for only cancels the await, it does not touch the process,
    so skipping this leaks a running `git clone` for every timed-out install.
    """
    try:
        proc.kill()
    except ProcessLookupError:
        return
    await proc.wait()


def _require_registry():
    registry = getattr(state, "app_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="AppRegistry not initialised")
    return registry


def _declared_dependencies(manifest) -> list[str]:
    """The manifest's skill_dependencies, trimmed, in declaration order."""
    raw = (
        manifest.get("skill_dependencies")
        if isinstance(manifest, dict)
        else getattr(manifest, "skill_dependencies", None)
    )
    return [
        dep.strip()
        for dep in list(raw or [])
        if isinstance(dep, str) and dep.strip()
    ]


def _installed_skill_ids() -> Optional[set[str]]:
    """Loaded skill ids, or None when this brain cannot say.

    None and the empty set are different answers: "no skills are loaded"
    licenses telling the user a dependency is missing, "there is no skill
    registry to ask" does not.
    """
    skill_registry = getattr(state, "skill_registry", None)
    if skill_registry is None:
        return None
    skills = getattr(skill_registry, "skills", None)
    if not isinstance(skills, Mapping):
        return None
    return set(skills)


def _missing_dependencies_for(manifest_dict: dict, declared: list[str]) -> list[dict]:
    """Declared dependencies that are not loaded right now, with what breaks.

    Computed live rather than stored. An app installed in a degraded state
    stops being degraded the moment the user installs the missing skill,
    and a row in a table would go on claiming otherwise.
    """
    present = _installed_skill_ids()
    if present is None:
        # Absence would be unproven, and an unproven warning is noise.
        return []
    out: list[dict] = []
    for skill_id in declared:
        if skill_id in present:
            continue
        out.append({
            "skill_id": skill_id,
            "impact": skill_dependency_impact(manifest_dict, skill_id),
            "remediation": _remediation_for(skill_id, ""),
        })
    return out


def _manifest_summary(app) -> dict:
    m = app.manifest
    manifest_dict = m.model_dump()
    declared = _declared_dependencies(m)
    return {
        "app_id": m.app_id,
        "version": m.version,
        "author": m.author,
        "description": m.description,
        "brand": m.brand.model_dump(),
        "entry_surface_id": m.entry_surface_id,
        "surfaces": [s.surface_id for s in m.surfaces],
        "contract": m.contract.model_dump() if getattr(m, "contract", None) else {},
        "permissions": (
            m.permissions if isinstance(m.permissions, dict) else list(m.permissions)
        ),
        "permission_details": describe_app_permissions(manifest_dict),
        "skill_dependencies": declared,
        # Live truth, not install-time truth. An app whose dependency
        # could not be verified installs anyway (see `preview_app`), so
        # the Apps page has to keep saying what is missing and how to get
        # it, not just say it once in an install response nobody kept.
        "missing_skill_dependencies": _missing_dependencies_for(manifest_dict, declared),
        "surface_schema_versions": {
            s.surface_id: s.schema_version for s in m.surfaces
        },
        "install_dir": str(app.install_dir),
        "installed_at": app.installed_at,
    }


# ─────────────────────────────────────────────
# Remediation: what to do about a dependency FERAL cannot install
# ─────────────────────────────────────────────

# Every ``command`` below is a real, current FERAL command. There is
# deliberately no command for the cases a command cannot fix: `feral
# install <id>` runs the same verifier that just refused the bundle, so
# printing it after a signature failure would send the user in a circle.
# There is also no CLI that installs a skill from a local directory --
# `feral install` / `feral marketplace install` take a published registry
# id, and `MarketplaceClient.install(source_url=...)` is reachable only
# from Python. Do not add advice here without running it first.


def _remediation_for(skill_id: str, reason: str) -> dict[str, str]:
    """Turn the brain's own refusal reason into something actionable."""
    text = (reason or "").lower()

    if "not published in the registry" in text or "not found in registry" in text:
        return {
            "code": "not_published",
            "message": (
                f"'{skill_id}' is not published in the FERAL registry, so there is "
                "nothing for FERAL to verify or download."
            ),
            "action": (
                "Ask the publisher of this app to publish the skill to "
                "registry.feral.sh. If you have its source and you trust it, you "
                "can publish it yourself: run `feral publisher login` once, then "
                "`feral publish --skill <dir>`, then install it with "
                f"`feral install {skill_id}`."
            ),
            # No single command: publishing needs a directory only the
            # user can name, and it needs a publisher identity first.
            # `feral publish` without one exits 401.
            "command": "",
        }
    if "pynacl" in text:
        return {
            "code": "verifier_missing",
            "message": (
                "FERAL cannot check any publisher signature on this machine "
                "because pynacl is not installed."
            ),
            "action": (
                "Install pynacl into the interpreter running the brain, restart "
                f"it, then install the skill with `feral install {skill_id}`."
            ),
            "command": "pip install pynacl",
        }
    if "unreachable" in text or "registry returned" in text or "download failed" in text:
        return {
            "code": "registry_unreachable",
            "message": (
                f"FERAL could not reach the registry to fetch '{skill_id}'. "
                "The skill may well be fine; the network is not."
            ),
            "action": (
                "Check the connection to registry.feral.sh and install the skill "
                "on its own once it is reachable."
            ),
            "command": f"feral install {skill_id}",
        }
    if "signature" in text or "sha256" in text or "verification" in text:
        return {
            "code": "signature_failed",
            "message": (
                f"The published bundle for '{skill_id}' does not match the "
                "publisher's signature, so FERAL cannot tell who wrote it or "
                "whether it was altered."
            ),
            "action": (
                "Retrying will not help: every install path runs this same check. "
                "The publisher has to re-publish a correctly signed bundle. Report "
                "it with the flag button on the registry listing."
            ),
            "command": "",
        }
    if "security check failed" in text or "invalid package" in text:
        return {
            "code": "package_refused",
            "message": (
                f"'{skill_id}' was downloaded and verified but failed FERAL's own "
                "package validation, so it was not installed."
            ),
            "action": (
                "This is a refusal on purpose, not a transient error. The "
                "publisher has to fix the package."
            ),
            "command": "",
        }
    if "marketplace" in text and "unavailable" in text:
        return {
            "code": "marketplace_unavailable",
            "message": (
                "This brain has no marketplace client wired, so it cannot install "
                "skills at all."
            ),
            "action": "Restart the brain; if it persists, run `feral doctor`.",
            "command": "feral doctor",
        }
    return {
        "code": "not_installed",
        "message": (
            f"'{skill_id}' is not installed on this brain."
            + (f" {reason}" if reason else "")
        ),
        "action": (
            "Install it on its own from the registry. The install shows its "
            "permissions and checks the publisher's signature first."
        ),
        "command": f"feral install {skill_id}",
    }


def _bound_daemon_for_session(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    bindings = getattr(state, "_daemon_session_bindings", {}) or {}
    for node_id, sessions in bindings.items():
        if session_id in sessions:
            return node_id
    return None


async def _maybe_push_genui_to_phone(session_id: str, result: dict[str, Any], *, title: str = "") -> None:
    node_id = _bound_daemon_for_session(session_id)
    if not node_id:
        return
    if not hasattr(state, "send_to_daemon"):
        return
    try:
        from models.protocol import FeralMessage
        payload = {
            "kind": "interactive",
            "push_id": result.get("screen_id", ""),
            "app_id": result.get("app_id", ""),
            "surface_id": result.get("surface_id", ""),
            "screen_id": result.get("screen_id", ""),
            "title": title or result.get("surface_id", "App"),
            "body": "",
            "sdui": result.get("root", {}),
        }
        await state.send_to_daemon(
            node_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="genui_push",
                payload=payload,
            ),
        )
    except Exception as exc:
        # It no longer fails silently. The phone showing nothing while the
        # caller is told the screen was pushed is the failure being fixed.
        logger.warning("genui_push relay failed: %s", exc)


@router.get("/api/apps")
async def list_apps():
    registry = _require_registry()
    apps = registry.list()
    return {
        "count": len(apps),
        "apps": [_manifest_summary(a) for a in apps],
    }


@router.get("/api/apps/{app_id}/manifest")
async def get_manifest(app_id: str):
    registry = _require_registry()
    app = registry.get(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail=f"app {app_id!r} not installed")
    return {
        "app_id": app.app_id,
        "version": app.version,
        "manifest": app.manifest.model_dump(),
        "install_dir": str(app.install_dir),
    }


class ValidateRequest(BaseModel):
    manifest: str  # raw YAML or JSON text


@router.post("/api/apps/validate")
async def validate_manifest(req: ValidateRequest):
    """Validate an AppManifest *without* installing it.

    Used by the v2 publisher flow so developers can lint their
    manifest against the exact same pydantic validator the registry
    and brain use at install time — no drift between "works locally"
    and "works when installed".
    """
    text = (req.manifest or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="manifest is empty")

    try:
        import yaml  # type: ignore
    except Exception:  # pragma: no cover
        yaml = None  # type: ignore

    data: Any
    first_error: Optional[str] = None
    try:
        import json
        data = json.loads(text)
    except Exception as json_err:
        first_error = str(json_err)
        if yaml is None:
            raise HTTPException(status_code=400, detail=f"invalid manifest: {first_error}")
        try:
            data = yaml.safe_load(text)
        except Exception as yaml_err:
            raise HTTPException(status_code=400, detail=f"invalid YAML/JSON: {yaml_err}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="manifest must be a mapping at the top level")

    try:
        from models.app_manifest import AppManifest
        manifest = AppManifest(**data)
    except Exception as exc:  # pydantic.ValidationError or value error
        raise HTTPException(status_code=400, detail=str(exc))

    actions: list[str] = []
    for surface in manifest.surfaces:
        for action in surface.action_contract:
            if action.action_id not in actions:
                actions.append(action.action_id)

    return {
        "success": True,
        "summary": {
            "app_id": manifest.app_id,
            "version": manifest.version,
            "contract": manifest.contract.model_dump(),
            "surfaces": [s.surface_id for s in manifest.surfaces],
            "actions": actions,
            "permissions": (
                manifest.permissions
                if isinstance(manifest.permissions, dict)
                else list(manifest.permissions)
            ),
            "skill_dependencies": list(getattr(manifest, "skill_dependencies", []) or []),
            "entry_surface_id": manifest.entry_surface_id,
            "surface_schema_versions": {
                s.surface_id: s.schema_version for s in manifest.surfaces
            },
        },
    }


class PreviewRequest(BaseModel):
    """Ask what installing something would do. Writes nothing."""

    # Exactly one of these should be set; the handler validates.
    path: Optional[str] = None
    git_url: Optional[str] = None
    registry_id: Optional[str] = None
    overwrite: bool = True
    # Trust gates wired through to AppRegistry. Default behaviour:
    # refuse unsigned bundles (signing is mandatory). Set
    # `unsigned=True` to opt into the local-dev flow that emits an
    # audit "unsigned_install" event without verification.
    unsigned: bool = False
    user_high_trust: bool = False
    # Registry acceptance gate override. When True AND the env flag
    # FERAL_INTERNAL_ALLOW_UNAPPROVED=1 is set on the brain, registry
    # installs may proceed for items that the registry has not yet
    # marked approved+public. Both knobs are required so neither a
    # stray request body nor a leaked env var alone can bypass the
    # gate.
    internal_override: bool = False


class InstallRequest(BaseModel):
    """Spend a token minted by ``POST /api/apps/preview``.

    The source is not read from here: it is inside the token, bound to
    the staged bytes the preview described. The source fields are still
    accepted so a caller that sends them gets told what changed instead
    of a schema error, and when present they must agree with the token.
    """

    install_token: str = ""
    path: Optional[str] = None
    git_url: Optional[str] = None
    registry_id: Optional[str] = None
    overwrite: bool = True


@dataclass
class _StagedAppSource:
    """A verified app bundle held between preview and install."""

    origin: str               # "path" | "git_url" | "registry_id"
    origin_value: str
    source_dir: Path
    manifest: dict[str, Any]
    signature: dict[str, Any]
    unsigned: bool
    user_high_trust: bool
    overwrite: bool
    stage_root: Optional[Path] = None
    registry_stage: Optional[RegistryStage] = None

    def cleanup(self) -> None:
        if self.registry_stage is not None:
            self.registry_stage.cleanup()
        if self.stage_root is not None:
            shutil.rmtree(self.stage_root, ignore_errors=True)


@dataclass
class _PendingAppInstall:
    payload: dict[str, Any]
    source: _StagedAppSource
    disclosure: dict[str, Any]
    # skill_id -> the marketplace install_token for that dependency.
    dep_tokens: dict[str, str] = field(default_factory=dict)


# nonce -> staged, verified app awaiting an install decision. Module
# state rather than AppRegistry state because it is a property of this
# HTTP conversation, not of the installed-apps index.
_pending_app_previews: dict[str, _PendingAppInstall] = {}


def _token_ref(token: str) -> str:
    """A loggable identifier for a consent token.

    The token itself is a credential and never goes in a log line. Its
    nonce is a public, single-use handle that is already in the pending
    map's keys, so it is what correlates a log line with a staged
    preview. An unreadable token gets a short digest instead, which is
    still enough to tell two failures apart.
    """
    payload = read_consent_token(token or "")
    if payload and payload.get("nonce"):
        return str(payload["nonce"])
    return "unreadable:" + hashlib.sha256((token or "").encode()).hexdigest()[:12]


def _release_dependency_previews(
    dep_tokens: Mapping[str, str], *, app_id: str, reason: str
) -> None:
    """Hand back staged skill bundles that will never be installed.

    The broad catch is deliberate and stays: this runs while an install
    is already failing or being abandoned, and letting a cleanup error
    propagate would replace the operator's real diagnosis ("the signature
    was bad") with a secondary one ("releasing a preview failed").
    Cleanup that destroys the diagnosis is worse than cleanup that fails.

    It logs at warning, not debug, and names the skill, the preview nonce
    and the app. A systematic failure to release previews leaks a staged
    tarball and one of MarketplaceClient's MAX_PENDING_PREVIEWS slots on
    every rollback, and at debug level nobody would learn that until the
    disk filled.
    """
    marketplace = getattr(state, "marketplace", None)
    if marketplace is None:
        return
    for skill_id, token in dep_tokens.items():
        try:
            marketplace.release_preview(token)
        except Exception as exc:
            logger.warning(
                "could not release the staged preview of skill %r (preview %s) "
                "while %s app %r; its tarball and its slot in the marketplace's "
                "pending map stay held until the 300s TTL sweeps them: %s",
                skill_id, _token_ref(token), reason, app_id, exc,
            )


def _drop_app_preview(nonce: str) -> Optional[_PendingAppInstall]:
    entry = _pending_app_previews.pop(nonce, None)
    if entry is None:
        return None
    entry.source.cleanup()
    _release_dependency_previews(
        entry.dep_tokens,
        app_id=str(entry.payload.get("app_id") or "?"),
        reason="expiring the preview of",
    )
    return entry


def _sweep_app_previews() -> None:
    """Expire old previews and bound how many stay open."""
    now = time.time()
    for nonce, entry in list(_pending_app_previews.items()):
        if entry.payload["exp"] <= now:
            _drop_app_preview(nonce)
    while len(_pending_app_previews) > MAX_PENDING_APP_PREVIEWS:
        oldest = min(_pending_app_previews.items(), key=lambda kv: kv[1].payload["iat"])
        _drop_app_preview(oldest[0])


def _claim_app_preview(install_token: str, req: InstallRequest) -> tuple[Optional[_PendingAppInstall], str]:
    """Validate a token and take the matching staged app.

    Refuses a token this process did not sign, one that has expired, one
    already spent, and one whose source disagrees with a source named in
    the request body.
    """
    payload = read_consent_token(install_token or "")
    if payload is None or payload.get("kind") != "app":
        return None, (
            "install requires an install_token from POST /api/apps/preview, so "
            "the app's permissions and the skills it installs are shown first"
        )
    if payload.get("exp", 0) <= time.time():
        _drop_app_preview(str(payload.get("nonce", "")))
        return None, "preview expired; review what this app installs again before installing"
    stated = {"path": req.path, "git_url": req.git_url, "registry_id": req.registry_id}
    named = stated.get(str(payload.get("origin") or ""))
    if named and str(named) != str(payload.get("origin_value") or ""):
        return None, "install_token does not match the source named in the request"
    for other_origin, value in stated.items():
        if value and other_origin != payload.get("origin"):
            return None, "install_token does not match the source named in the request"
    entry = _pending_app_previews.get(str(payload.get("nonce", "")))
    if entry is None:
        return None, "install_token already used or no longer held"
    if entry.payload != payload:
        return None, "install_token does not match the staged app"
    _pending_app_previews.pop(str(payload["nonce"]), None)
    return entry, ""


def _verified_install_kwargs(req) -> dict:
    return {
        "allow_unsigned": bool(req.unsigned),
        "user_high_trust": bool(req.user_high_trust),
        "overwrite": req.overwrite,
        "vault": getattr(state, "vault", None),
        "supervisor": getattr(state, "supervisor", None),
    }


def _install_with_signing(registry, source: str, *, req: "_StagedAppSource"):
    """Run install_app and translate exceptions into HTTP envelopes.

    422 — verification refused (UnverifiedManifestError or PolicyViolation).
    400 — anything else from the install path.

    ``req`` carries the trust flags the *preview* was answered with, not
    whatever the install request body says, so a caller cannot preview a
    signed bundle and then install it with ``unsigned=true``.
    """
    try:
        return registry.install_app(source, **_verified_install_kwargs(req))
    except UnverifiedManifestError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unverified_manifest",
                "message": str(exc),
                "remediation": (
                    "Sign the manifest with `feral app sign <manifest_path>` "
                    "or pass `unsigned=true` to opt into an unsigned install."
                ),
            },
        )
    except PolicyViolation as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "permissions_policy_violation",
                "message": str(exc),
                "remediation": (
                    "Narrow `permissions.network` to specific origins, or "
                    "supply `permissions.justification` AND set "
                    "`user_high_trust=true` on a signed install."
                ),
            },
        )


async def _clone_git_repo(git_url: str, dest: Path) -> None:
    """Shallow-clone *git_url* into *dest* without blocking the loop.

    AUDIT-FIXES F-05. This was subprocess.run(timeout=120) inside an
    async handler, so a slow or hostile remote froze the whole event
    loop for up to two minutes: voice streaming, websocket heartbeats
    and every other in-flight request stopped with it. It now runs in
    the preview step rather than the install step, because the bytes a
    user consents to have to be the bytes that get installed and a
    second clone could fetch a different commit.
    """
    clone_cmd = ["git", "clone", "--depth", "1", git_url, str(dest)]
    proc = await asyncio.create_subprocess_exec(
        *clone_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=GIT_CLONE_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        # subprocess.run kills the child before raising. Without this the
        # abandoned clone keeps writing into a directory being deleted.
        await _terminate(proc)
        raise subprocess.TimeoutExpired(clone_cmd, GIT_CLONE_TIMEOUT_S) from None
    if proc.returncode != 0:
        stderr = stderr_b.decode(errors="replace") if stderr_b else ""
        raise HTTPException(
            status_code=400,
            detail=f"git clone failed: {stderr.strip()[:400]}",
        )


async def _stage_source(registry, req: PreviewRequest) -> _StagedAppSource:
    """Fetch + verify an app bundle into a directory this route owns.

    A local path is copied and a git URL is cloned rather than read in
    place, so an install cannot pick up an edit made while the consent
    dialog was on screen.
    """
    if req.registry_id:
        stage: RegistryStage = await asyncio.to_thread(
            registry.stage_registry_bundle,
            req.registry_id,
            registry_url=None,
            allow_unsigned=bool(req.unsigned),
            internal_override=bool(req.internal_override),
        )
        try:
            # The policy gate depends on user_high_trust, so it belongs to
            # the decision, not the bytes. Run it now so a refusal happens
            # before anything is shown rather than after confirm.
            enforce_install_policy(
                stage.manifest,
                allow_unsigned=not stage.verified_signature,
                user_high_trust=bool(req.user_high_trust),
            )
        except Exception:
            stage.cleanup()
            raise
        return _StagedAppSource(
            origin="registry_id",
            origin_value=str(req.registry_id),
            source_dir=stage.source_dir,
            manifest=stage.manifest,
            signature={
                "verified": bool(stage.verified_signature),
                "sha256": str(stage.item.get("sha256") or ""),
                "publisher": str(stage.item.get("publisher") or ""),
                "reason": "" if stage.verified_signature else "bundle is not signed",
            },
            unsigned=bool(req.unsigned),
            user_high_trust=bool(req.user_high_trust),
            overwrite=bool(req.overwrite),
            registry_stage=stage,
        )

    stage_root = Path(tempfile.mkdtemp(prefix="feral-app-preview-"))
    source_dir = stage_root / "bundle"
    try:
        if req.git_url:
            await _clone_git_repo(str(req.git_url), source_dir)
            origin, origin_value = "git_url", str(req.git_url)
        else:
            local = Path(str(req.path)).expanduser().resolve()
            if not local.is_dir():
                raise HTTPException(status_code=400, detail=f"not a directory: {local}")
            await asyncio.to_thread(shutil.copytree, local, source_dir)
            origin, origin_value = "path", str(req.path)

        report = await asyncio.to_thread(
            lambda: registry.inspect_app(
                source_dir,
                allow_unsigned=bool(req.unsigned),
                user_high_trust=bool(req.user_high_trust),
                vault=getattr(state, "vault", None),
            )
        )
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, stage_root, ignore_errors=True)
        raise

    return _StagedAppSource(
        origin=origin,
        origin_value=origin_value,
        source_dir=source_dir,
        manifest=report["manifest"],
        signature=report["signature"],
        unsigned=bool(req.unsigned),
        user_high_trust=bool(req.user_high_trust),
        overwrite=bool(req.overwrite),
        stage_root=stage_root,
    )


async def _describe_dependencies(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Say which skills this app pulls in, and what each one can reach.

    Three buckets, because they are three different decisions:

    * ``already_installed`` — nothing new runs. Listed so the app's full
      reach is visible, but it is not what the user is being asked about.
    * ``to_install`` — new code, verified through the marketplace's
      preview so the permission list comes out of a bundle whose
      publisher signature checked out. Each carries a single-use
      marketplace token the install spends.
    * ``unavailable`` — FERAL could not verify or find it. Named, with
      the brain's own reason, what it would have been used for, and what
      to do about it.
    """
    declared = _declared_dependencies(manifest)
    disclosure: dict[str, Any] = {
        "declared": declared,
        "already_installed": [],
        "to_install": [],
        "unavailable": [],
    }
    dep_tokens: dict[str, str] = {}
    if not declared:
        return disclosure, dep_tokens

    skill_registry = getattr(state, "skill_registry", None)
    marketplace = getattr(state, "marketplace", None)
    # A brain that cannot say what is loaded resolves the dependency
    # rather than assuming it is already there: the failure mode of
    # installing a skill twice is smaller than the failure mode of an
    # app whose actions have nothing to call.
    present = _installed_skill_ids() or set()

    for skill_id in declared:
        if skill_id in present:
            installed_manifest = getattr(skill_registry, "skills", {}).get(skill_id)
            from models.skill_manifest import describe_permissions, permission_values

            perms = permission_values(getattr(installed_manifest, "permissions", []) or [])
            disclosure["already_installed"].append({
                "skill_id": skill_id,
                "name": getattr(getattr(installed_manifest, "brand", None), "name", "") or skill_id,
                "version": str(getattr(installed_manifest, "version", "") or ""),
                "permissions": perms,
                "permission_details": describe_permissions(perms),
            })
            continue

        if marketplace is None:
            reason = "marketplace unavailable on this brain"
            disclosure["unavailable"].append({
                "skill_id": skill_id,
                "reason": reason,
                "remediation": _remediation_for(skill_id, reason),
                "impact": skill_dependency_impact(manifest, skill_id),
            })
            continue

        try:
            preview = await marketplace.preview_from_registry("skill", skill_id)
        except Exception as exc:
            # Broad on purpose: whatever went wrong resolving one
            # dependency, the other dependencies and the app itself are
            # still describable, and the user is better served by a sheet
            # that names this one as unavailable than by a 500. It becomes
            # user-visible copy below, and it is logged here because a
            # systematic resolution failure is a brain-side problem the
            # operator will not see in a per-app dialog.
            logger.warning(
                "resolving skill dependency %r of app %r failed: %s",
                skill_id, manifest.get("app_id") or "?", exc,
            )
            preview = {"success": False, "error": str(exc)}

        if not preview.get("success") or not preview.get("install_token"):
            reason = str(preview.get("error") or "could not be verified")
            disclosure["unavailable"].append({
                "skill_id": skill_id,
                "reason": reason,
                "remediation": _remediation_for(skill_id, reason),
                "impact": skill_dependency_impact(manifest, skill_id),
            })
            continue

        dep_tokens[skill_id] = str(preview["install_token"])
        disclosure["to_install"].append({
            "skill_id": skill_id,
            "name": preview.get("name") or skill_id,
            "version": preview.get("version") or "",
            "publisher": preview.get("publisher") or "",
            "description": preview.get("description") or "",
            "permissions": list(preview.get("permissions") or []),
            "permission_details": list(preview.get("permission_details") or []),
            "signature": preview.get("signature") or {},
        })

    return disclosure, dep_tokens


@router.post("/api/apps/preview")
async def preview_app(req: PreviewRequest, unsigned: Optional[bool] = None):
    """Step one of installing an app: say what it is and what it brings.

    Verifies the bundle, reads the manifest out of it, describes the
    app's own reach, resolves every declared skill dependency and mints
    a single-use ``install_token``. Nothing is installed here.

    A dependency FERAL cannot verify does **not** end the flow. It is
    disclosed with the brain's reason, the actions that will not work
    without it, and what to do about it, and the token still issues. An
    app that is useful without one of its skills is a better outcome
    than a refusal that leaves the user with nothing and no next step;
    the install response and the Apps page both keep saying what is
    missing, so the state stays truthful while it is degraded.
    """
    registry = _require_registry()
    sources_set = sum(1 for v in (req.path, req.git_url, req.registry_id) if v)
    if sources_set == 0:
        raise HTTPException(status_code=400, detail="provide one of: path, git_url, registry_id")
    if sources_set > 1:
        raise HTTPException(status_code=400, detail="provide exactly one of: path, git_url, registry_id")

    # Query-string `?unsigned=true` shadows the body field so curl-y
    # callers don't have to re-encode JSON to opt in.
    if unsigned is not None:
        req = req.model_copy(update={"unsigned": bool(unsigned)})

    try:
        source = await _stage_source(registry, req)
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        # Propagated rather than folded into a 400, which is the
        # behaviour the clone has always had. Changing it is a separate
        # decision from adding the consent gate.
        raise
    except UnverifiedManifestError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unverified_manifest",
                "message": str(exc),
                "remediation": (
                    "Sign the manifest with `feral app sign <manifest_path>` "
                    "or pass `unsigned=true` to opt into an unsigned install."
                ),
            },
        )
    except UnapprovedRegistryItemError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "registry_item_not_approved",
                "message": str(exc),
                "remediation": (
                    "Wait for FERAL org reviewers to approve this submission. "
                    "Internal users with prior coordination may bypass with "
                    "FERAL_INTERNAL_ALLOW_UNAPPROVED=1 AND internal_override=true "
                    "on the request."
                ),
            },
        )
    except PolicyViolation as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "permissions_policy_violation",
                "message": str(exc),
                "remediation": (
                    "Narrow `permissions.network` to specific origins, or supply "
                    "`permissions.justification` AND set `user_high_trust=true` "
                    "on a signed install."
                ),
            },
        )
    except AppRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        disclosure, dep_tokens = await _describe_dependencies(source.manifest)
    except BaseException:
        source.cleanup()
        raise

    manifest = source.manifest
    permission_details = describe_app_permissions(manifest)
    nonce = secrets.token_urlsafe(16)
    now = time.time()
    payload = {
        "v": 1,
        "kind": "app",
        "app_id": str(manifest.get("app_id") or ""),
        "origin": source.origin,
        "origin_value": source.origin_value,
        "manifest_sha256": manifest_digest(manifest),
        "app_permissions": sorted(row["id"] for row in permission_details),
        "signed": bool(source.signature.get("verified")),
        # The skill tokens ride inside this payload, so the outer MAC
        # covers them: a dependency cannot be swapped for a different
        # one after the list has been read.
        "skills_new": sorted(dep_tokens),
        "skill_tokens": [dep_tokens[k] for k in sorted(dep_tokens)],
        "skills_present": sorted(d["skill_id"] for d in disclosure["already_installed"]),
        "skills_unavailable": sorted(d["skill_id"] for d in disclosure["unavailable"]),
        "nonce": nonce,
        "iat": now,
        "exp": now + APP_PREVIEW_TTL_SECONDS,
    }
    _pending_app_previews[nonce] = _PendingAppInstall(
        payload=payload,
        source=source,
        disclosure=disclosure,
        dep_tokens=dep_tokens,
    )
    _sweep_app_previews()

    return {
        "success": True,
        "app": {
            "app_id": manifest.get("app_id"),
            "version": manifest.get("version"),
            "author": manifest.get("author"),
            "description": manifest.get("description"),
            "brand": manifest.get("brand") or {},
            "entry_surface_id": manifest.get("entry_surface_id"),
            "surfaces": [
                {"surface_id": s.get("surface_id"), "kind": s.get("kind")}
                for s in (manifest.get("surfaces") or [])
                if isinstance(s, dict)
            ],
        },
        "source": {"origin": source.origin, "value": source.origin_value},
        "signature": source.signature,
        "permissions": manifest.get("permissions") or [],
        "permission_details": permission_details,
        "skill_dependencies": disclosure,
        "degraded": bool(disclosure["unavailable"]),
        "install_token": sign_consent_token(payload),
        "expires_in": APP_PREVIEW_TTL_SECONDS,
    }


async def _install_dependencies(entry: _PendingAppInstall) -> dict[str, Any]:
    """Install the skills the preview disclosed, over the verified path.

    ``MarketplaceClient.install_from_registry`` spends the token minted
    during the preview, so the bytes installed are the bytes whose
    permissions were on screen. The unverified
    ``MarketplaceClient.install`` is not reachable from here, which is
    the whole point: it performs no signature check, and a skill runs
    Python in this process.
    """
    marketplace = getattr(state, "marketplace", None)
    skill_registry = getattr(state, "skill_registry", None)
    report: dict[str, Any] = {
        "declared": list(entry.disclosure["declared"]),
        "already_present": [d["skill_id"] for d in entry.disclosure["already_installed"]],
        "installed": [],
        "failed": [],
        "unavailable": list(entry.disclosure["unavailable"]),
    }
    for skill_id, token in entry.dep_tokens.items():
        if marketplace is None:
            report["failed"].append({
                "skill_id": skill_id,
                "error": "marketplace unavailable on this brain",
            })
            continue
        try:
            result = await marketplace.install_from_registry(
                "skill", skill_id, install_token=token
            )
        except Exception as exc:
            # Broad so one bad dependency is reported as a failed
            # dependency rather than a 500 with no list. It is a typed
            # entry in `failed`, which rolls the app back and reaches the
            # caller with remediation; the log line is here because this
            # is a skill that verified minutes ago and then did not
            # install, which is worth investigating server-side.
            logger.warning(
                "skill %r verified at preview time then failed to install "
                "(preview %s): %s",
                skill_id, _token_ref(token), exc,
            )
            report["failed"].append({"skill_id": skill_id, "error": str(exc)})
            continue
        if not result.get("success"):
            report["failed"].append({
                "skill_id": skill_id,
                "error": str(result.get("error") or "install failed"),
            })
            continue
        report["installed"].append(skill_id)
        if skill_registry is not None and skill_id not in getattr(skill_registry, "skills", {}):
            try:
                skill_registry.reload_skill(skill_id)
            except Exception as exc:
                logger.warning("skill %s installed but did not load: %s", skill_id, exc)
    return report


@router.post("/api/apps/install")
async def install_app(req: InstallRequest):
    """Step two: install exactly the app the preview described.

    Requires an ``install_token`` from ``POST /api/apps/preview``. There
    is no ungated path: an app install writes SDUI surfaces AND installs
    the skills the app depends on, and skills execute Python in this
    process, so an unattended install is an unattended code install.
    """
    registry = _require_registry()
    if not req.install_token:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "preview_required",
                "message": (
                    "install requires an install_token from POST /api/apps/preview, "
                    "so the app's permissions and the skills it installs are shown "
                    "before anything is installed."
                ),
                "remediation": (
                    "POST /api/apps/preview with the same source, show the result, "
                    "then POST /api/apps/install with the install_token it returns."
                ),
            },
        )

    _sweep_app_previews()
    entry, error = _claim_app_preview(req.install_token, req)
    if entry is None:
        raise HTTPException(
            status_code=403,
            detail={"error": "preview_required", "message": error},
        )

    source = entry.source

    def _abandon() -> None:
        """Drop the staged app and the dependency previews it was holding.

        The dependency tokens are spent by ``_install_dependencies`` on
        the success path. When the app itself fails to install they are
        never spent, and without this they would hold their tarballs and
        their slots in the marketplace's pending map until the TTL swept
        them.
        """
        source.cleanup()
        _release_dependency_previews(
            entry.dep_tokens,
            app_id=str(entry.payload.get("app_id") or "?"),
            reason="rolling back the install of",
        )

    try:
        if source.origin == "registry_id":
            app = await asyncio.to_thread(
                lambda: registry.install_staged_registry(
                    source.registry_stage,
                    user_high_trust=source.user_high_trust,
                    overwrite=(req.overwrite and source.overwrite),
                    supervisor=getattr(state, "supervisor", None),
                )
            )
        else:
            app = await asyncio.to_thread(
                lambda: _install_with_signing(registry, str(source.source_dir), req=source)
            )
    except HTTPException:
        _abandon()
        raise
    except UnverifiedManifestError as exc:
        _abandon()
        raise HTTPException(
            status_code=422,
            detail={"error": "unverified_manifest", "message": str(exc)},
        )
    except PolicyViolation as exc:
        _abandon()
        raise HTTPException(
            status_code=422,
            detail={"error": "permissions_policy_violation", "message": str(exc)},
        )
    except AppRegistryError as exc:
        _abandon()
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _abandon()
        raise HTTPException(status_code=400, detail=str(exc))

    dep_report = await _install_dependencies(entry)
    source.cleanup()

    failed = list(dep_report.get("failed") or [])
    if failed:
        # These are dependencies that verified during the preview and
        # then failed to install anyway. That is a broken invariant
        # rather than a choice the user made, and the existing stance
        # (keep installed state truthful) applies: roll the app back.
        # A dependency that was *disclosed* as unavailable took the
        # consented path above and never reaches here.
        registry.uninstall(app.app_id, purge_cache=False)
        raise HTTPException(
            status_code=422,
            detail={
                "error": "skill_dependency_install_failed",
                "app_id": app.app_id,
                "failed": [
                    {**f, "remediation": _remediation_for(f["skill_id"], f.get("error", ""))}
                    for f in failed
                ],
                "message": (
                    "These skills verified when the install was previewed and then "
                    f"failed to install, so {app.app_id} was rolled back rather than "
                    "left half-working."
                ),
                "remediation": (
                    "Install the skills listed above on their own, then install the "
                    "app again."
                ),
            },
        )

    return {
        "success": True,
        "app": _manifest_summary(app),
        "skill_dependencies": dep_report,
        "degraded": bool(dep_report.get("unavailable")),
    }


@router.delete("/api/apps/{app_id}")
async def uninstall_app(app_id: str):
    registry = _require_registry()
    removed = registry.uninstall(app_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"app {app_id!r} not installed")
    return {"success": True}


class OpenSurfaceRequest(BaseModel):
    surface_id: Optional[str] = None
    data: dict = Field(default_factory=dict)
    session_id: str = ""
    user_fingerprint: str = "default"
    regenerate: bool = False


@router.post("/api/apps/{app_id}/open")
async def open_app(app_id: str, req: OpenSurfaceRequest):
    """Render a surface AND push it over the active session WebSocket.

    If the caller supplies a ``session_id`` that matches a live v2
    session, the brain emits an ``sdui`` message so the UI mounts the
    tree inline. Otherwise the payload is returned in the response
    body for the caller to render itself.
    """
    registry = _require_registry()
    app = registry.get(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail=f"app {app_id!r} not installed")
    surface_id = req.surface_id or app.manifest.entry_surface_id
    try:
        result = await registry.open_surface(
            app_id=app_id,
            surface_id=surface_id,
            session_id=req.session_id,
            data=req.data,
            user_fingerprint=req.user_fingerprint,
            regenerate=req.regenerate,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Optional push over the active WebSocket.
    pushed: bool | None = None
    push_error = ""
    if req.session_id and state.sessions and req.session_id in state.sessions:
        try:
            from models.protocol import FeralMessage, SDUIPayload
            await state.send_to_session(
                req.session_id,
                FeralMessage(
                    session_id=req.session_id,
                    hop="brain",
                    type="sdui",
                    payload=SDUIPayload(
                        screen_id=result["screen_id"],
                        root=result["root"],
                    ).model_dump(),
                ),
            )
            await _maybe_push_genui_to_phone(
                req.session_id,
                result,
                title=f"{app_id} · {surface_id}",
            )
            pushed = True
        except Exception as exc:
            # The render did succeed, so success stays True. But the
            # caller asked for the surface to be mounted in its session
            # and that did not happen. This comment used to claim the
            # response said so; only the server log did, so a client
            # whose push failed saw an unqualified success and waited for
            # a surface that was never going to arrive.
            logger.warning("push to session failed: %s", exc)
            pushed = False
            push_error = str(exc)

    out = {"success": True, **result}
    if pushed is not None:
        out["pushed"] = pushed
        if push_error:
            out["push_error"] = push_error
    return out


class RenderSurfaceRequest(BaseModel):
    data: dict = Field(default_factory=dict)
    user_fingerprint: str = "default"
    regenerate: bool = False


@router.post("/api/apps/{app_id}/surfaces/{surface_id}/render")
async def render_surface(app_id: str, surface_id: str, req: RenderSurfaceRequest):
    registry = _require_registry()
    app = registry.get(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail=f"app {app_id!r} not installed")
    try:
        result = await registry.open_surface(
            app_id=app_id,
            surface_id=surface_id,
            session_id="",
            data=req.data,
            user_fingerprint=req.user_fingerprint,
            regenerate=req.regenerate,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, **result}


class DispatchRequest(BaseModel):
    surface_id: str
    action_id: str
    value: Any = None
    event: str = "tap"
    session_id: str = ""


@router.post("/api/apps/{app_id}/dispatch")
async def dispatch_action(app_id: str, req: DispatchRequest):
    """Dispatch a ui_event via REST (parity with WebSocket ui_event).

    Validates the action against the surface contract, returns 400 on
    drift, then forwards to the orchestrator exactly the same way the
    WS path does. Used by external CLIs / tests that don't want to
    keep a live WebSocket open.
    """
    registry = _require_registry()
    try:
        spec = registry.validate_action(
            app_id,
            req.surface_id,
            req.action_id,
            value=req.value,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if state.orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")

    screen_id = registry.build_screen_id(
        app_id=app_id,
        surface_id=req.surface_id,
        scope=req.session_id or "rest-dispatch",
    )
    try:
        await state.orchestrator.handle_ui_event(
            session_id=req.session_id or "rest-dispatch",
            action_id=req.action_id,
            event=req.event,
            value=req.value,
            app_id=app_id,
            screen_id=screen_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "success": True,
        "handler": spec.handler,
        "target": spec.target,
        "requires_confirmation": bool(spec.requires_confirmation),
        "screen_id": screen_id,
    }
