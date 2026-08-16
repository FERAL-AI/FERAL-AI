"""
FERAL Skill Package — Validation and dependency management
=============================================================
Handles skill package structure, security validation,
and dependency resolution for marketplace-installed skills.
"""

from __future__ import annotations
import ast
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from config.loader import feral_home
from models.skill_manifest import SkillManifest, describe_permissions, permission_values

logger = logging.getLogger("feral.skills.package")

SKILLS_DIR = feral_home() / "skills"


class SkillPackage:
    """Represents a skill package on disk."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.manifest: Optional[SkillManifest] = None
        self.errors: list[str] = []

    @property
    def manifest_path(self) -> Path:
        return self.path / "manifest.json"

    @property
    def impl_path(self) -> Path:
        return self.path / "impl.py"

    @property
    def readme_path(self) -> Path:
        return self.path / "README.md"

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0 and self.manifest is not None

    def load(self) -> bool:
        """Load and validate the package."""
        self.errors.clear()

        if not self.path.is_dir():
            self.errors.append(f"Package directory not found: {self.path}")
            return False

        if not self.manifest_path.exists():
            self.errors.append("Missing manifest.json")
            return False

        try:
            with open(self.manifest_path) as f:
                data = json.load(f)
            self.manifest = SkillManifest(**data)
        except Exception as e:
            self.errors.append(f"Invalid manifest.json: {e}")
            return False

        return True

    def get_metadata(self) -> dict:
        if not self.manifest:
            return {}
        permissions = permission_values(self.manifest.permissions)
        return {
            "skill_id": self.manifest.skill_id,
            "name": self.manifest.brand.name,
            "description": self.manifest.description,
            "version": getattr(self.manifest, "version", "0.1.0"),
            "source": "marketplace",
            "path": str(self.path),
            "has_impl": self.impl_path.exists(),
            "has_readme": self.readme_path.exists(),
            # What this skill can reach. Every consumer of this dict (the
            # installed list, the install response, the consent dialog)
            # reads the same two keys, and the human-readable copy is
            # served with the ids so no client has to invent wording.
            "permissions": permissions,
            "permission_details": describe_permissions(permissions),
        }


class SkillValidator:
    """Security checks for skill packages."""

    DANGEROUS_CALLS = {
        "os.system", "os.popen", "subprocess.call", "subprocess.run",
        "subprocess.Popen", "exec", "eval", "__import__",
        "shutil.rmtree", "os.remove", "os.unlink",
    }

    DANGEROUS_IMPORTS = {
        "ctypes", "socket",
    }

    def __init__(self):
        self._allowed_domains: list[str] = []

    def validate(self, package: SkillPackage) -> list[str]:
        """Run all validation checks. Returns list of warnings/errors."""
        issues = []

        if not package.load():
            return package.errors

        if package.impl_path.exists():
            code_issues = self._check_python_code(package.impl_path)
            issues.extend(code_issues)

        manifest_issues = self._check_manifest(package)
        issues.extend(manifest_issues)

        return issues

    def _check_python_code(self, path: Path) -> list[str]:
        """Static analysis for dangerous patterns."""
        issues = []
        try:
            source = path.read_text()
        except Exception as e:
            return [f"Cannot read {path}: {e}"]

        for call in self.DANGEROUS_CALLS:
            if call in source:
                issues.append(f"SECURITY: Found potentially dangerous call '{call}' in {path.name}")

        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in self.DANGEROUS_IMPORTS:
                            issues.append(f"SECURITY: Suspicious import '{alias.name}' in {path.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[0] in self.DANGEROUS_IMPORTS:
                        issues.append(f"SECURITY: Suspicious import from '{node.module}' in {path.name}")
        except SyntaxError as e:
            issues.append(f"Syntax error in {path.name}: {e}")

        return issues

    def _check_manifest(self, package: SkillPackage) -> list[str]:
        issues = []
        m = package.manifest
        if not m:
            return ["No manifest loaded"]

        if not m.skill_id or not re.match(r"^[a-z0-9_]+$", m.skill_id):
            issues.append(f"Invalid skill_id: '{m.skill_id}' — must be lowercase alphanumeric with underscores")

        if not m.description:
            issues.append("Missing description in manifest")

        for ep in m.endpoints:
            if ep.url and not ep.url.startswith(("http://", "https://", "/")):
                if ep.method != "WS_EXECUTE":
                    issues.append(f"Suspicious URL in endpoint '{ep.id}': {ep.url}")

        return issues


def install_package(source_path: Path, target_dir: Path = None) -> SkillPackage:
    """Install a skill package to the skills directory."""
    target_dir = target_dir or SKILLS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    pkg = SkillPackage(source_path)
    if not pkg.load():
        raise ValueError(f"Invalid package: {pkg.errors}")

    dest = target_dir / pkg.manifest.skill_id
    if dest.exists():
        shutil.rmtree(dest)

    shutil.copytree(source_path, dest)

    installed = SkillPackage(dest)
    installed.load()
    return installed


def uninstall_package(skill_id: str, target_dir: Path = None) -> bool:
    """Remove an installed skill."""
    target_dir = target_dir or SKILLS_DIR
    dest = target_dir / skill_id
    if dest.exists():
        shutil.rmtree(dest)
        return True
    return False


def list_installed(target_dir: Path = None) -> list[SkillPackage]:
    """List all installed marketplace skills."""
    target_dir = target_dir or SKILLS_DIR
    if not target_dir.exists():
        return []

    packages = []
    for d in sorted(target_dir.iterdir()):
        if d.is_dir() and (d / "manifest.json").exists():
            pkg = SkillPackage(d)
            pkg.load()
            packages.append(pkg)
    return packages
