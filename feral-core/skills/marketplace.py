"""
FERAL Skill Marketplace — Discover, install, and manage community skills
===========================================================================
HTTP client for the FERAL registry (or GitHub-based index).
Skills are downloaded, validated, and registered with the SkillRegistry.
"""

from __future__ import annotations
import asyncio
import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from config.runtime import market_registry_url
from models.skill_manifest import describe_permissions, permission_values
from skills.package import (
    SkillPackage,
    SkillValidator,
    SKILLS_DIR,
    install_package,
    uninstall_package,
    list_installed,
)

logger = logging.getLogger("feral.marketplace")

DEFAULT_REGISTRY_URL = market_registry_url()

# How long a preview stays installable, and how many may be outstanding.
# The window is short because the whole point of the token is that the
# thing the user read about is the thing that gets installed.
PREVIEW_TTL_SECONDS = 300
MAX_PENDING_PREVIEWS = 16

# The kinds `cli.install.dispatch_install` knows how to place. Checked
# before a preview is issued because dispatch_install answers an unknown
# kind with sys.exit(1), which is correct in a CLI and would take the
# brain down inside a request handler.
INSTALLABLE_KINDS = (
    "skill", "daemon", "mcp", "channel", "provider", "memory", "workflow", "agent",
)

# Per-process HMAC key for preview tokens. Generated at import and never
# written anywhere: a token is only meaningful to the brain that issued
# it, and a restart invalidates every outstanding preview, which is the
# correct behaviour for a consent artefact.
_PREVIEW_SECRET = secrets.token_bytes(32)

# cli/install.py's verifier prints its reason and exits. Capturing that
# reason means redirecting stdout, which is process-global, so the two
# call sites that do it are serialised and kept short. See _run_cli_gate.
_CLI_GATE_LOCK = threading.Lock()


class MarketplaceError(Exception):
    """A registry lookup or download failed in a way worth reporting."""


def _run_cli_gate(fn, *args):
    """Run one of ``cli.install``'s gates and translate its CLI contract.

    ``cli.install._verify`` and ``cli.install._safe_extract`` are the
    verified install path: they are what ``feral install`` runs, they
    are what the registry's signing scheme was written against, and they
    fail by printing a reason and calling ``sys.exit(1)``. That contract
    is right for a terminal and wrong for a request handler, but a
    second implementation of signature checking is a second thing that
    can drift, so the UI path calls these and translates rather than
    reimplementing them.

    Returns ``(ok, reason, value)``. ``logging`` output is unaffected by
    the redirect: ``StreamHandler`` holds its own reference to the
    stream it was constructed with.
    """
    buf = io.StringIO()
    with _CLI_GATE_LOCK:
        try:
            with contextlib.redirect_stdout(buf):
                value = fn(*args)
        except SystemExit:
            reason = buf.getvalue().strip().splitlines()
            return False, (reason[-1].strip() if reason else "verification failed"), None
    return True, "", value


def _verify_bundle(item: dict, tarball: Path) -> tuple[bool, str, str]:
    """SHA-256 + Ed25519 check via ``cli.install._verify``."""
    from cli import install as cli_install

    if not cli_install._NACL_AVAILABLE:
        return False, "signature verification unavailable: pynacl is not installed", ""
    ok, reason, digest = _run_cli_gate(cli_install._verify, item, tarball)
    if not ok:
        return False, reason, ""
    return True, "", digest.hex()


def _extract_bundle(tarball: Path, dest: Path) -> tuple[bool, str]:
    """Path-escape-safe extraction via ``cli.install._safe_extract``."""
    from cli import install as cli_install

    ok, reason, _ = _run_cli_gate(cli_install._safe_extract, tarball, dest)
    return ok, reason


def _sign_preview(payload: dict) -> str:
    """Sign a preview payload so it cannot be edited or invented.

    The payload names the kind, the item id, the SHA-256 of the exact
    verified tarball, the permission list that was rendered, whether the
    signature verified, a single-use nonce and an expiry. Replay for a
    different package is not possible because the id and the digest are
    inside the MAC: a token issued for A does not authenticate a request
    naming B, and the key never leaves this process.
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(_PREVIEW_SECRET, raw, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(raw).decode().rstrip("=")
        + "."
        + base64.urlsafe_b64encode(mac).decode().rstrip("=")
    )


def _read_preview(token: str) -> Optional[dict]:
    """Return the payload of a token this process signed, else None."""
    if not token or not isinstance(token, str) or token.count(".") != 1:
        return None
    body_b64, mac_b64 = token.split(".")
    try:
        raw = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        mac = base64.urlsafe_b64decode(mac_b64 + "=" * (-len(mac_b64) % 4))
    except Exception:
        return None
    expected = hmac.new(_PREVIEW_SECRET, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None

# Wall-clock budget for `git pull` in the update path. Named so the
# timeout-and-kill behaviour is testable without a 30s test.
GIT_PULL_TIMEOUT_S = 30

GITHUB_INDEX_URL = os.getenv(
    "FERAL_MARKETPLACE_GITHUB",
    "https://raw.githubusercontent.com/FERAL-AI/feral-skills/main/index.json",
)


def _safe_extract_zip(zf, dest: str) -> None:
    """Extract a zip, refusing any member that escapes ``dest``.

    ``ZipFile.extractall`` does sanitize absolute paths and ``..`` on its
    own, but it is silent about it and it does not check symlinks, whose
    target is stored in the member data rather than the name. A skill
    archive is untrusted input downloaded from a registry or a GitHub
    index, so a member that resolves outside the destination is treated
    as hostile and fails the whole install rather than being quietly
    dropped. ``tarfile`` gets the equivalent via ``filter="data"``, which
    is Python 3.14's default and not 3.11's.
    """
    root = Path(dest).resolve()
    for member in zf.infolist():
        target = (root / member.filename).resolve()
        if target != root and root not in target.parents:
            raise ValueError(
                f"refusing archive member that escapes the extract directory: "
                f"{member.filename!r}"
            )
        # 0xA000 is S_IFLNK in the high 16 bits of external_attr, which is
        # how zip stores a symlink. Its target lives in the file body, so
        # the name check above cannot see where it points.
        if (member.external_attr >> 16) & 0xF000 == 0xA000:
            raise ValueError(
                f"refusing symlink in skill archive: {member.filename!r}"
            )
    zf.extractall(dest)


class MarketplaceClient:
    """HTTP client for skill discovery and installation."""

    def __init__(self, registry_url: str = None, skill_registry=None):
        self._registry_url = registry_url or DEFAULT_REGISTRY_URL
        self._client = httpx.AsyncClient(timeout=30.0)
        self._validator = SkillValidator()
        self._skill_registry = skill_registry
        # nonce -> staged, verified preview awaiting an install decision.
        self._pending_previews: dict[str, dict] = {}

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search published skills."""
        try:
            resp = await self._client.get(
                f"{self._registry_url}/skills/search",
                params={"q": query, "limit": limit},
            )
            if resp.status_code == 200:
                return resp.json().get("results", [])
        except Exception:
            pass

        return await self._search_github_index(query, limit)

    async def _search_github_index(self, query: str, limit: int) -> list[dict]:
        """Fallback: search the GitHub-hosted skill index."""
        try:
            resp = await self._client.get(GITHUB_INDEX_URL)
            if resp.status_code == 200:
                index = resp.json()
                skills = index.get("skills", [])
                query_lower = query.lower()
                results = [
                    s for s in skills
                    if query_lower in s.get("name", "").lower()
                    or query_lower in s.get("description", "").lower()
                    or query_lower in " ".join(s.get("tags", [])).lower()
                ]
                return results[:limit]
        except Exception as e:
            logger.debug(f"GitHub index search failed: {e}")

        return []

    # ─────────────────────────────────────────────
    # Verified registry install (preview -> consent -> install)
    # ─────────────────────────────────────────────

    async def _registry_fetch_item(self, item_id: str) -> dict:
        """``GET /api/v1/item/{id}`` walking the registry fallback list."""
        from cli.publish import registry_base_urls

        bases = registry_base_urls()
        last_error = ""
        for base in bases:
            url = f"{base}/api/v1/item/{item_id}"
            try:
                resp = await self._client.get(url, follow_redirects=True)
            except Exception as exc:
                last_error = f"registry unreachable at {base}: {exc}"
                logger.debug("registry item fetch failed for %s: %s", url, exc)
                continue
            if resp.status_code == 404:
                raise MarketplaceError(f"'{item_id}' is not published in the registry at {base}")
            if resp.status_code >= 400:
                last_error = f"registry returned {resp.status_code} for {item_id}"
                continue
            try:
                data = resp.json()
            except Exception as exc:
                raise MarketplaceError(f"registry returned a non-JSON item record: {exc}") from exc
            if not isinstance(data, dict):
                raise MarketplaceError("registry returned an unexpected item record")
            return data
        raise MarketplaceError(last_error or f"registry unreachable (tried {', '.join(bases)})")

    async def _registry_download(self, url: str) -> Path:
        """Stream a bundle to a temp file. Bytes are untrusted until verified."""
        stage = Path(tempfile.mkdtemp(prefix="feral-preview-dl-"))
        out = stage / "bundle.tar.gz"
        try:
            async with self._client.stream("GET", url, follow_redirects=True) as resp:
                if resp.status_code >= 400:
                    raise MarketplaceError(f"bundle download failed ({resp.status_code}) from {url}")
                with open(out, "wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
        except MarketplaceError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(stage, ignore_errors=True)
            raise MarketplaceError(f"bundle download failed: {exc}") from exc
        return out

    def _drop_preview(self, nonce: str) -> Optional[dict]:
        entry = self._pending_previews.pop(nonce, None)
        if entry:
            shutil.rmtree(entry["stage_root"], ignore_errors=True)
        return entry

    def _sweep_previews(self) -> None:
        """Expire old previews and bound the number held open."""
        now = time.time()
        for nonce, entry in list(self._pending_previews.items()):
            if entry["payload"]["exp"] <= now:
                self._drop_preview(nonce)
        while len(self._pending_previews) > MAX_PENDING_PREVIEWS:
            oldest = min(self._pending_previews.items(), key=lambda kv: kv[1]["payload"]["iat"])
            self._drop_preview(oldest[0])

    async def preview_from_registry(self, kind: str, item_id: str) -> dict:
        """Fetch, verify and describe a registry item without installing it.

        This is the first half of the two-step install. It downloads the
        bundle, verifies SHA-256 + Ed25519 through the same code path as
        ``feral install``, unpacks it into a staging directory, reads the
        manifest **out of the verified archive** (the ``manifest`` field
        in the registry's JSON response is not covered by the signature,
        so it is metadata, not evidence) and returns the permission list
        with the signature status.

        The staged tree is kept, keyed by the token's nonce, so the
        install step writes exactly the bytes the user was shown. Nothing
        under ``~/.feral`` is touched until the token comes back.
        """
        kind = (kind or "skill").lower()
        if kind not in INSTALLABLE_KINDS:
            return {"success": False, "error": f"unknown item kind '{kind}'"}
        try:
            item = await self._registry_fetch_item(item_id)
        except MarketplaceError as exc:
            return {"success": False, "error": str(exc)}

        item_kind = (item.get("kind") or (item.get("manifest") or {}).get("kind") or kind).lower()
        if item_kind not in INSTALLABLE_KINDS:
            return {"success": False, "error": f"registry reports unknown item kind '{item_kind}'"}
        download_url = item.get("download_url")
        if not download_url:
            return {"success": False, "error": "registry record has no download_url"}

        try:
            tarball = await self._registry_download(str(download_url))
        except MarketplaceError as exc:
            return {"success": False, "error": str(exc)}
        stage_root = tarball.parent

        ok, reason, digest_hex = await asyncio.to_thread(_verify_bundle, item, tarball)
        if not ok:
            shutil.rmtree(stage_root, ignore_errors=True)
            return {
                "success": False,
                "error": reason or "signature verification failed",
                "signature": {"verified": False, "reason": reason, "sha256": "", "publisher": ""},
            }

        extract_dir = stage_root / "extracted"
        ok, reason = await asyncio.to_thread(_extract_bundle, tarball, extract_dir)
        if not ok:
            shutil.rmtree(stage_root, ignore_errors=True)
            return {"success": False, "error": reason or "bundle could not be unpacked"}

        registry_manifest = item.get("manifest") or {}
        permissions: list[str] = []
        manifest_source = "registry"
        warnings: list[str] = []
        pkg_dir: Optional[Path] = None
        name = registry_manifest.get("name") or (registry_manifest.get("brand") or {}).get("name") or item_id
        version = str(registry_manifest.get("version", "?"))
        description = registry_manifest.get("description", "")

        if item_kind == "skill":
            manifests = sorted(extract_dir.rglob("manifest.json"))
            if not manifests:
                shutil.rmtree(stage_root, ignore_errors=True)
                return {"success": False, "error": "bundle contains no manifest.json"}
            pkg_dir = manifests[0].parent
            pkg = SkillPackage(pkg_dir)
            if not pkg.load():
                shutil.rmtree(stage_root, ignore_errors=True)
                return {"success": False, "error": f"invalid package: {'; '.join(pkg.errors)}"}
            # Validation runs here, on the staging copy, before anything
            # is written to the skills directory.
            issues = await asyncio.to_thread(self._validator.validate, pkg)
            security_issues = [i for i in issues if "SECURITY" in i]
            if security_issues:
                shutil.rmtree(stage_root, ignore_errors=True)
                return {"success": False, "error": f"security check failed: {'; '.join(security_issues)}"}
            warnings = issues
            permissions = permission_values(pkg.manifest.permissions)
            manifest_source = "package"
            name = pkg.manifest.brand.name or name
            version = pkg.manifest.version
            description = pkg.manifest.description or description
            item_id_for_install = pkg.manifest.skill_id
        else:
            permissions = list(registry_manifest.get("permissions") or [])
            item_id_for_install = item_id

        nonce = secrets.token_urlsafe(16)
        now = time.time()
        payload = {
            "v": 1,
            "kind": item_kind,
            "id": item_id,
            "sha256": digest_hex,
            "perms": sorted(permissions),
            "verified": True,
            "nonce": nonce,
            "iat": now,
            "exp": now + PREVIEW_TTL_SECONDS,
        }
        self._pending_previews[nonce] = {
            "payload": payload,
            "stage_root": stage_root,
            "tarball": tarball,
            "pkg_dir": pkg_dir,
            "manifest": registry_manifest,
            "install_id": item_id_for_install,
            "permissions": permissions,
            "warnings": warnings,
            "publisher": item.get("publisher") or item.get("publisher_login") or "",
        }
        self._sweep_previews()

        return {
            "success": True,
            "kind": item_kind,
            "id": item_id,
            "name": name,
            "version": version,
            "description": description,
            "publisher": item.get("publisher") or item.get("publisher_login") or "",
            "permissions": permissions,
            "permission_details": describe_permissions(permissions),
            "permissions_source": manifest_source,
            "warnings": warnings,
            "signature": {
                "verified": True,
                "sha256": digest_hex,
                "publisher": item.get("publisher") or "",
                "publisher_key": str(item.get("publisher_pubkey_hex") or item.get("publisher_pubkey") or ""),
                "reason": "",
            },
            "install_token": _sign_preview(payload),
            "expires_in": PREVIEW_TTL_SECONDS,
        }

    def _claim_preview(self, kind: str, item_id: str, install_token: str) -> tuple[Optional[dict], str]:
        """Validate a token and take the matching staged preview.

        Refuses a token that is not ours, has expired, has already been
        spent, or names a different package than the one being asked
        for. The staged bundle is removed from the pending map before
        the install runs, so a token is good for exactly one install.
        """
        payload = _read_preview(install_token or "")
        if payload is None:
            return None, (
                "install requires a preview token: POST /api/marketplace/preview first "
                "so the permissions and signature can be shown"
            )
        if payload.get("exp", 0) <= time.time():
            self._drop_preview(str(payload.get("nonce", "")))
            return None, "preview expired; review the permissions again before installing"
        if payload.get("id") != item_id or payload.get("kind") != (kind or "skill").lower():
            return None, "preview token does not match the requested package"
        entry = self._pending_previews.get(str(payload.get("nonce", "")))
        if entry is None:
            return None, "preview token already used or no longer held"
        if entry["payload"] != payload:
            return None, "preview token does not match the staged package"
        self._pending_previews.pop(str(payload["nonce"]), None)
        return entry, ""

    async def install_from_registry(
        self,
        kind: str,
        item_id: str,
        install_token: str = "",
    ) -> dict:
        """Install a registry item the user has previewed and approved.

        Second half of the two-step install. There is no unverified
        fallback: if the bundle did not verify, ``preview_from_registry``
        never issued a token and this never runs.
        """
        kind = (kind or "skill").lower()
        entry, error = self._claim_preview(kind, item_id, install_token)
        if entry is None:
            return {"success": False, "error": error, "code": "preview_required"}

        stage_root: Path = entry["stage_root"]
        try:
            if kind == "skill":
                pkg_dir = entry["pkg_dir"]
                installed = await asyncio.to_thread(install_package, pkg_dir, SKILLS_DIR)
                if self._skill_registry and installed.manifest:
                    self._skill_registry.register(installed.manifest)
                result = {
                    "success": True,
                    "kind": kind,
                    "skill_id": installed.manifest.skill_id if installed.manifest else entry["install_id"],
                    "path": str(installed.path),
                }
            else:
                from cli.install import dispatch_install

                home = Path(os.environ.get("FERAL_HOME") or (Path.home() / ".feral"))
                # Through _run_cli_gate, not directly: dispatch_install
                # reports a refused member or an unknown kind with
                # sys.exit(1), and a SystemExit raised inside a request
                # handler takes the worker with it.
                ok, reason, _ = await asyncio.to_thread(
                    _run_cli_gate,
                    dispatch_install, kind, entry["manifest"], entry["tarball"], entry["install_id"], home,
                )
                if not ok:
                    return {"success": False, "error": reason or f"{kind} install failed"}
                result = {"success": True, "kind": kind, "id": entry["install_id"]}
        except Exception as exc:
            logger.warning("verified install of %s failed after consent: %s", item_id, exc)
            return {"success": False, "error": f"install failed: {exc}"}
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)

        result.update({
            "permissions": entry["permissions"],
            "permission_details": describe_permissions(entry["permissions"]),
            "warnings": entry["warnings"],
            "signature": {"verified": True, "sha256": entry["payload"]["sha256"]},
        })
        return result

    # ─────────────────────────────────────────────
    # Developer / CLI install paths
    # ─────────────────────────────────────────────

    async def install(self, skill_id: str, version: str = "latest", source_url: str = None) -> dict:
        """Download and install a skill package.

        Unverified by construction: it takes whatever the URL or the
        GitHub index hands back. This is the local-iteration path for a
        developer working on their own skill from a shell, which is why
        it is not reachable from ``POST /api/marketplace/install`` -- the
        browser goes through preview + token instead.
        """
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        if source_url:
            logger.warning(
                "UNVERIFIED INSTALL: skill '%s' is being installed from %s with no "
                "signature check. This is the developer path; anything published to "
                "the registry must be installed with `feral install %s`, which "
                "verifies the publisher's Ed25519 signature.",
                skill_id, source_url, skill_id,
            )
            return await self._install_from_url(skill_id, source_url)

        try:
            resp = await self._client.get(f"{self._registry_url}/skills/{skill_id}/download", params={"version": version})
            if resp.status_code == 200:
                return await self._install_from_archive(skill_id, resp.content)
        except Exception:
            pass

        return await self._install_from_github(skill_id)

    async def _install_from_url(self, skill_id: str, url: str) -> dict:
        """Install from a direct URL (git repo or archive)."""
        if url.endswith(".git") or "github.com" in url:
            return self._install_from_git(skill_id, url)
        resp = await self._client.get(url)
        if resp.status_code == 200:
            return await self._install_from_archive(skill_id, resp.content)
        return {"success": False, "error": f"Failed to download from {url}"}

    def _install_from_git(self, skill_id: str, git_url: str) -> dict:
        """Clone a git repo as a skill."""
        import subprocess

        dest = SKILLS_DIR / skill_id
        if dest.exists():
            shutil.rmtree(dest)

        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", git_url, str(dest)],
                capture_output=True, text=True, check=True, timeout=60,
            )

            pkg = SkillPackage(dest)
            if not pkg.load():
                shutil.rmtree(dest, ignore_errors=True)
                return {"success": False, "error": f"Invalid package: {pkg.errors}"}

            issues = self._validator.validate(pkg)
            security_issues = [i for i in issues if "SECURITY" in i]
            if security_issues:
                shutil.rmtree(dest, ignore_errors=True)
                return {"success": False, "error": f"Security check failed: {security_issues}"}

            if self._skill_registry:
                self._skill_registry.register(pkg.manifest)

            return {"success": True, "skill_id": skill_id, "path": str(dest), "warnings": issues}

        except Exception as e:
            shutil.rmtree(dest, ignore_errors=True)
            return {"success": False, "error": str(e)}

    async def _install_from_github(self, skill_id: str) -> dict:
        """Try to install from the GitHub skills index."""
        try:
            resp = await self._client.get(GITHUB_INDEX_URL)
            if resp.status_code == 200:
                index = resp.json()
                for skill in index.get("skills", []):
                    if skill.get("id") == skill_id:
                        repo_url = skill.get("repository", "")
                        if repo_url:
                            return self._install_from_git(skill_id, repo_url)
        except Exception as e:
            logger.debug(f"GitHub index fetch failed: {e}")

        return {"success": False, "error": f"Skill '{skill_id}' not found in registry or GitHub index"}

    async def _install_from_archive(self, skill_id: str, archive_bytes: bytes) -> dict:
        """Install from a tar.gz or zip archive."""
        import tarfile
        import zipfile
        import io

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                if archive_bytes[:2] == b"PK":
                    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                        _safe_extract_zip(zf, tmpdir)
                else:
                    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tf:
                        # ``filter="data"`` rejects absolute paths, parent
                        # traversal, device nodes, symlinks pointing outside
                        # the destination, and setuid bits. It is the
                        # default from Python 3.14; we are on 3.11 where the
                        # unfiltered call is the CVE-2007-4559 behaviour.
                        tf.extractall(tmpdir, filter="data")

                # Find the manifest
                tmp_path = Path(tmpdir)
                manifest_files = list(tmp_path.rglob("manifest.json"))
                if not manifest_files:
                    return {"success": False, "error": "No manifest.json found in archive"}

                pkg_dir = manifest_files[0].parent

                # Validate in the temp directory, before anything is
                # copied into the skills directory. This used to run the
                # other way round -- install_package copied the tree to
                # ~/.feral/skills and the validator's verdict was then
                # discarded -- so a package that failed its security
                # check was already on disk and already loadable.
                staged = SkillPackage(pkg_dir)
                if not staged.load():
                    return {"success": False, "error": f"Invalid package: {'; '.join(staged.errors)}"}

                issues = self._validator.validate(staged)
                security_issues = [i for i in issues if "SECURITY" in i]
                if security_issues:
                    return {"success": False, "error": f"Security check failed: {security_issues}"}

                pkg = install_package(pkg_dir, SKILLS_DIR)
                if self._skill_registry and pkg.manifest:
                    self._skill_registry.register(pkg.manifest)

                return {
                    "success": True,
                    "skill_id": skill_id,
                    "path": str(pkg.path),
                    "warnings": issues,
                    "permissions": permission_values(pkg.manifest.permissions) if pkg.manifest else [],
                }

            except Exception as e:
                return {"success": False, "error": str(e)}

    async def uninstall(self, skill_id: str) -> dict:
        """Remove an installed skill."""
        removed = uninstall_package(skill_id)
        if removed:
            if self._skill_registry and skill_id in self._skill_registry.skills:
                del self._skill_registry.skills[skill_id]
                self._skill_registry._tool_cache.pop(skill_id, None)
            return {"success": True, "skill_id": skill_id}
        return {"success": False, "error": f"Skill '{skill_id}' not found"}

    async def update(self, skill_id: str) -> dict:
        """Fetch latest version of an installed skill.

        NOT the verified path. A git-backed skill is `git pull`ed and a
        registry-backed one falls through to :meth:`install`, neither of
        which checks a publisher signature and neither of which re-asks
        the user, so an update can widen a skill's permissions without
        anyone seeing the new list. Installing is gated by
        :meth:`preview_from_registry`; updating is not, and closing that
        gap needs an update-consent flow of its own (WORK-ORDER P0.2/P0.3
        cover install only).
        """
        pkg_dir = SKILLS_DIR / skill_id
        if not pkg_dir.exists():
            return {"success": False, "error": f"Skill '{skill_id}' not installed"}

        git_dir = pkg_dir / ".git"
        if git_dir.exists():
            import subprocess
            try:
                # AUDIT-FIXES F-05. subprocess.run(timeout=30) here ran on the
                # event loop thread, so a slow remote stalled every other
                # coroutine for up to 30s. check=True has no equivalent on
                # create_subprocess_exec, so the CalledProcessError is raised
                # by hand below to keep the returned error string identical.
                pull_cmd = ["git", "pull", "--ff-only"]
                proc = await asyncio.create_subprocess_exec(
                    *pull_cmd,
                    cwd=str(pkg_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=GIT_PULL_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    # Kill the child, as subprocess.run's timeout did. An
                    # abandoned `git pull` would keep writing into the skill
                    # directory we are about to re-read.
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
                    raise subprocess.TimeoutExpired(pull_cmd, GIT_PULL_TIMEOUT_S) from None
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(
                        proc.returncode if proc.returncode is not None else -1,
                        pull_cmd,
                        output=stdout_b.decode(errors="replace") if stdout_b else "",
                        stderr=stderr_b.decode(errors="replace") if stderr_b else "",
                    )
                pkg = SkillPackage(pkg_dir)
                if pkg.load() and self._skill_registry:
                    self._skill_registry.register(pkg.manifest)
                return {"success": True, "skill_id": skill_id, "method": "git_pull"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        logger.warning(
            "UNVERIFIED UPDATE: skill '%s' has no git checkout, so it is being "
            "refetched over the unsigned download/GitHub-index path. The "
            "publisher signature is not checked and the user is not shown the "
            "new permission list.",
            skill_id,
        )
        return await self.install(skill_id)

    def list_installed(self) -> list[dict]:
        """List all marketplace-installed skills."""
        packages = list_installed()
        return [pkg.get_metadata() for pkg in packages if pkg.valid]

    async def close(self):
        for nonce in list(self._pending_previews):
            self._drop_preview(nonce)
        await self._client.aclose()
