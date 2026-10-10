"""Bounded desktop build profiles; selection does not mutate or download."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import struct
import subprocess

PINNED_OPENCODE_VERSION = "1.18.10"


class BundlePlatformError(ValueError):
    """Unsupported or unverifiable build input, without private file contents."""


@dataclass(frozen=True)
class BundlePlatform:
    package: str
    npm_os: str
    npm_cpu: str
    binary_format: str
    machine: int


def select_platform(system: str, machine: str, libc: str = "") -> BundlePlatform:
    if system == "Darwin" and machine == "arm64":
        return BundlePlatform("opencode-darwin-arm64", "darwin", "arm64", "mach-o", 0x0100000C)
    if system == "Darwin" and machine == "x86_64":
        return BundlePlatform("opencode-darwin-x64", "darwin", "x64", "mach-o", 0x01000007)
    if system == "Linux" and machine == "x86_64" and libc == "glibc":
        return BundlePlatform("opencode-linux-x64-baseline", "linux", "x64", "elf", 62)
    raise BundlePlatformError("Unsupported desktop build profile: macOS arm64/x86_64 or Linux x86_64 glibc is required.")


def host_platform() -> BundlePlatform:
    system = platform.system()
    libc = platform.libc_ver()[0] if system == "Linux" else ""
    return select_platform(system, platform.machine(), libc)


def validate_binary_header(header: bytes, selected: BundlePlatform) -> None:
    if selected.binary_format == "elf":
        if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", header, 16)[0] not in (2, 3) or struct.unpack_from("<H", header, 18)[0] != selected.machine:
            raise BundlePlatformError("OpenCode must be a matching little-endian ELF64 executable.")
    elif len(header) < 8 or header[:4] != b"\xcf\xfa\xed\xfe" or struct.unpack_from("<I", header, 4)[0] != selected.machine:
        raise BundlePlatformError("OpenCode must be a matching single-architecture Mach-O executable.")


def validate_package(package_root: Path, selected: BundlePlatform, *, probe_version: bool = True) -> Path:
    if package_root.name != selected.package or package_root.is_symlink():
        raise BundlePlatformError("OpenCode package identity does not match the selected profile.")
    metadata_path = package_root / "package.json"
    try:
        if metadata_path.is_symlink() or metadata_path.stat().st_size > 65_536:
            raise BundlePlatformError("OpenCode package metadata is unsupported.")
        with metadata_path.open("rb") as stream:
            encoded = stream.read(65_537)
        if len(encoded) > 65_536:
            raise BundlePlatformError("OpenCode package metadata exceeds its budget.")
        metadata = json.loads(encoded)
    except (OSError, ValueError) as error:
        raise BundlePlatformError("OpenCode package metadata could not be verified.") from error
    if not isinstance(metadata, dict) or metadata.get("name") != selected.package or metadata.get("version") != PINNED_OPENCODE_VERSION or metadata.get("os") != [selected.npm_os] or metadata.get("cpu") != [selected.npm_cpu]:
        raise BundlePlatformError("OpenCode package metadata does not match the pinned target.")
    binary = package_root / "bin" / "opencode"
    try:
        if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK) or not 20 <= binary.stat().st_size <= 512 * 1024**2:
            raise BundlePlatformError("OpenCode executable is missing or unsupported.")
        with binary.open("rb") as stream:
            validate_binary_header(stream.read(64), selected)
    except OSError as error:
        raise BundlePlatformError("OpenCode executable could not be inspected.") from error
    if probe_version:
        try:
            result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BundlePlatformError("OpenCode version probe failed or exceeded its deadline.") from error
        if result.returncode != 0 or result.stdout.strip() != PINNED_OPENCODE_VERSION:
            raise BundlePlatformError("OpenCode version does not match the pinned runtime.")
    return binary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select-host", action="store_true")
    parser.add_argument("--validate-package", type=Path)
    args = parser.parse_args()
    try:
        selected = host_platform()
        if args.select_host and args.validate_package is None:
            print(selected.package)
        elif args.validate_package is not None and not args.select_host:
            validate_package(args.validate_package, selected)
            print("Verified pinned OpenCode package and executable target.")
        else:
            parser.error("Choose exactly one selection or validation operation.")
    except BundlePlatformError as error:
        parser.exit(1, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
