"""
FERAL Skill Marketplace — Discover, install, and manage community skills
===========================================================================
HTTP client for the FERAL registry (or GitHub-based index).
Skills are downloaded, validated, and registered with the SkillRegistry.
"""

from __future__ import annotations
import ipaddress
import json
import logging
import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from config.runtime import market_registry_url
from skills.package import (
    SkillPackage,
    SkillValidator,
    SKILLS_DIR,
    install_package,
    uninstall_package,
    list_installed,
)

logger = logging.getLogger("feral.marketplace")


def _assert_safe_url(url: str) -> None:
    """SSRF guard: allow only http(s) URLs whose host resolves to public IPs.

    Rejects non-http(s) schemes and any host that resolves to a
    private/loopback/link-local/reserved/multicast address (which covers the
    cloud metadata endpoint 169.254.169.254).
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsafe URL scheme '{parsed.scheme or ''}': only http/https allowed")
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL has no host: {url}")

    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve host '{host}': {e}")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Blocked SSRF target: host '{host}' resolves to non-public IP {ip}"
            )


def _assert_safe_member(name: str, dest: Path) -> None:
    """Reject archive members that would escape the extraction directory."""
    if not name:
        return
    p = Path(name)
    if p.is_absolute():
        raise ValueError(f"Unsafe archive member (absolute path): {name}")
    if ".." in p.parts:
        raise ValueError(f"Unsafe archive member (parent traversal): {name}")
    target = (dest / name).resolve()
    if target != dest and dest not in target.parents:
        raise ValueError(f"Unsafe archive member (escapes extraction dir): {name}")


def _safe_extract_zip(zf, dest: Path) -> None:
    dest = dest.resolve()
    for member in zf.infolist():
        _assert_safe_member(member.filename, dest)
        mode = (member.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ValueError(f"Unsafe archive member (symlink): {member.filename}")
    zf.extractall(dest)


def _safe_extract_tar(tf, dest: Path) -> None:
    dest = dest.resolve()
    members = tf.getmembers()
    for m in members:
        _assert_safe_member(m.name, dest)
        if m.issym() or m.islnk():
            raise ValueError(f"Unsafe archive member (link): {m.name}")
    tf.extractall(dest, members=members)

DEFAULT_REGISTRY_URL = market_registry_url()

GITHUB_INDEX_URL = os.getenv(
    "FERAL_MARKETPLACE_GITHUB",
    "https://raw.githubusercontent.com/FERAL-AI/feral-skills/main/index.json",
)


class MarketplaceClient:
    """HTTP client for skill discovery and installation."""

    def __init__(self, registry_url: str = None, skill_registry=None):
        self._registry_url = registry_url or DEFAULT_REGISTRY_URL
        self._client = httpx.AsyncClient(timeout=30.0)
        self._validator = SkillValidator()
        self._skill_registry = skill_registry

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

    async def install(self, skill_id: str, version: str = "latest", source_url: str = None) -> dict:
        """Download and install a skill package."""
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        if source_url:
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
        try:
            _assert_safe_url(url)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if url.endswith(".git") or "github.com" in url:
            return self._install_from_git(skill_id, url)
        resp = await self._client.get(url)
        if resp.status_code == 200:
            return await self._install_from_archive(skill_id, resp.content)
        return {"success": False, "error": f"Failed to download from {url}"}

    def _install_from_git(self, skill_id: str, git_url: str) -> dict:
        """Clone a git repo as a skill."""
        import subprocess

        try:
            _assert_safe_url(git_url)
        except ValueError as e:
            return {"success": False, "error": str(e)}

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
            tmp_path = Path(tmpdir)
            try:
                # Safe-extract: reject any member that escapes the tmpdir
                # (Zip-Slip / tar traversal), is absolute, or is a symlink.
                if archive_bytes[:2] == b"PK":
                    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                        _safe_extract_zip(zf, tmp_path)
                else:
                    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tf:
                        _safe_extract_tar(tf, tmp_path)

                # Find the manifest
                manifest_files = list(tmp_path.rglob("manifest.json"))
                if not manifest_files:
                    return {"success": False, "error": "No manifest.json found in archive"}

                pkg_dir = manifest_files[0].parent

                # Validate BEFORE copying into SKILLS_DIR; block on SECURITY
                # findings (mirrors the git install path).
                staged = SkillPackage(pkg_dir)
                if not staged.load():
                    return {"success": False, "error": f"Invalid package: {staged.errors}"}

                issues = self._validator.validate(staged)
                security_issues = [i for i in issues if "SECURITY" in i]
                if security_issues:
                    return {"success": False, "error": f"Security check failed: {security_issues}"}

                pkg = install_package(pkg_dir, SKILLS_DIR)
                if self._skill_registry and pkg.manifest:
                    self._skill_registry.register(pkg.manifest)

                return {"success": True, "skill_id": skill_id, "path": str(pkg.path), "warnings": issues}

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
        """Fetch latest version of an installed skill."""
        pkg_dir = SKILLS_DIR / skill_id
        if not pkg_dir.exists():
            return {"success": False, "error": f"Skill '{skill_id}' not installed"}

        git_dir = pkg_dir / ".git"
        if git_dir.exists():
            import subprocess
            try:
                subprocess.run(
                    ["git", "pull", "--ff-only"],
                    cwd=str(pkg_dir), capture_output=True, text=True, check=True, timeout=30,
                )
                pkg = SkillPackage(pkg_dir)
                if pkg.load() and self._skill_registry:
                    self._skill_registry.register(pkg.manifest)
                return {"success": True, "skill_id": skill_id, "method": "git_pull"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        return await self.install(skill_id)

    def list_installed(self) -> list[dict]:
        """List all marketplace-installed skills."""
        packages = list_installed()
        return [pkg.get_metadata() for pkg in packages if pkg.valid]

    async def close(self):
        await self._client.aclose()
