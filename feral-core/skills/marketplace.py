"""
FERAL Skill Marketplace — Discover, install, and manage community skills
===========================================================================
HTTP client for the FERAL registry (or GitHub-based index).
Skills are downloaded, validated, and registered with the SkillRegistry.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

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

DEFAULT_REGISTRY_URL = market_registry_url()

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
                pkg = install_package(pkg_dir, SKILLS_DIR)

                issues = self._validator.validate(pkg)
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

        return await self.install(skill_id)

    def list_installed(self) -> list[dict]:
        """List all marketplace-installed skills."""
        packages = list_installed()
        return [pkg.get_metadata() for pkg in packages if pkg.valid]

    async def close(self):
        await self._client.aclose()
