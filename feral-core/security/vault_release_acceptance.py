"""Version-bound acceptance for fresh OS credential storage in signed bundles.

An environment variable or API request cannot enable this gate. Release tooling
must place an acceptance receipt in the app's signed Info.plist after actual OS
acceptance. Development and ad-hoc bundles remain unavailable. Reading this
receipt never queries or writes a Keychain item.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import plistlib
import re
import subprocess
import sys


@dataclass(frozen=True)
class VaultReleaseAcceptance:
    accepted: bool
    code: str


def _verified_developer_team(bundle: Path) -> str | None:
    requirement = (
        "anchor apple generic and "
        "certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
    )
    try:
        verified = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict",
             "--test-requirement", requirement, str(bundle)],
            capture_output=True, timeout=5, check=False,
        )
        if verified.returncode != 0:
            return None
        displayed = subprocess.run(
            ["/usr/bin/codesign", "--display", "--verbose=4", str(bundle)],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if displayed.returncode != 0 or len(displayed.stderr) > 65536:
        return None
    metadata = displayed.stderr.decode("utf-8", errors="replace")
    if "Authority=Developer ID Application:" not in metadata:
        return None
    match = re.search(r"^TeamIdentifier=([A-Z0-9]{10})$", metadata, re.MULTILINE)
    return match.group(1) if match else None


def inspect_vault_release_acceptance(
    *, interpreter: Path | None = None,
    verify_team: Callable[[Path], str | None] = _verified_developer_team,
) -> VaultReleaseAcceptance:
    """Derive the signed app from its interpreter; no caller-supplied app path.

    Injectable interpreter/verifier arguments are for isolated acceptance tests;
    production calls use the actual process executable and signature verifier.
    A signature verifies the receipt's publisher/integrity, not OS acceptance:
    the release process must perform that acceptance before issuing the receipt.
    """
    refused = VaultReleaseAcceptance(False, "release_acceptance_required")
    try:
        executable = (interpreter or Path(sys.executable)).resolve()
    except (OSError, RuntimeError):
        return refused
    bundles = [parent for parent in executable.parents if parent.suffix == ".app"]
    if len(bundles) != 1:
        return refused
    bundle = bundles[0]
    runtime = bundle / "Contents" / "Resources" / "python"
    if not executable.is_relative_to(runtime):
        return refused
    try:
        info_path = bundle / "Contents" / "Info.plist"
        if info_path.is_symlink() or info_path.stat().st_size > 65536:
            return refused
        info = plistlib.loads(info_path.read_bytes())
        if not isinstance(info, dict):
            return refused
        receipt = info.get("FERALVaultInitializationAcceptance")
        if not isinstance(receipt, dict) or receipt.get("accepted") is not True:
            return refused
        team = receipt.get("team_id")
        acceptance_id = receipt.get("acceptance_id")
        if (
            receipt.get("scope") != "macos-keychain-add-if-absent"
            or receipt.get("contract_version") != "1"
            or receipt.get("bundle_id") != info.get("CFBundleIdentifier")
            or not isinstance(info.get("CFBundleIdentifier"), str)
            or receipt.get("build") != info.get("CFBundleVersion")
            or not isinstance(info.get("CFBundleVersion"), str)
            or not isinstance(team, str)
            or re.fullmatch(r"[A-Z0-9]{10}", team) is None
            or not isinstance(acceptance_id, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", acceptance_id) is None
        ):
            return refused
    except (OSError, ValueError, plistlib.InvalidFileException):
        return refused
    if verify_team(bundle) != team:
        return refused
    return VaultReleaseAcceptance(True, "signed_release_accepted")
