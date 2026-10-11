"""Identity-gated manual 9.30 acceptance launch, never a release certification.

The coordinator must supply the actual full source SHA and executable SHA256.
No GUI is launched by importing this module or by using --check-only.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sys
from datetime import datetime, timezone

import populated_profile_probe

ROOT = Path("/private/tmp/feral-native-populated-saved-context-20261002")
APP = Path(__file__).resolve().parents[1] / "build/FERAL Native Preview.app"
MANIFEST = Path("/private/tmp/feral-candidate-9-30-manifest.json")
VERSION = "2026.9.30"
BUILD = "2026100204"


def validate_expectations(expected_source, expected_sha):
    """Pure fail-closed validator, also used by the read-only observer."""
    if not re.fullmatch(r"[0-9a-f]{40}", expected_source or ""):
        raise ValueError("An explicit full lowercase source SHA is required")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""):
        raise ValueError("An explicit lowercase executable SHA256 is required")


def validate_identity(info, manifest, digest, expected_source, expected_sha):
    """Pure fail-closed validator, also used by the read-only observer."""
    validate_expectations(expected_source, expected_sha)
    if not isinstance(info, dict) or not isinstance(manifest, dict):
        raise ValueError("Candidate metadata must be objects")
    if (info.get("CFBundleShortVersionString"), info.get("CFBundleVersion"),
            info.get("CFBundleIdentifier")) != (VERSION, BUILD, "ai.feral.native.preview"):
        raise ValueError("Not the expected 9.30 candidate metadata")
    expected = {"version": VERSION, "build": BUILD, "runtime_source": expected_source,
                "native_sha256": expected_sha}
    if any(manifest.get(key) != value for key, value in expected.items()) or digest != expected_sha:
        raise ValueError("Candidate differs from the coordinator's exact READY identity")
    count = manifest.get("core_equality_files")
    if type(count) is not int or count < 1:
        raise ValueError("Missing coordinator production-core comparison receipt")
    return {**expected, "core_equality_files": count}


def candidate_identity(expected_source, expected_sha):
    validate_expectations(expected_source, expected_sha)
    info_path = APP / "Contents/Info.plist"
    if not info_path.is_file() or info_path.stat().st_size > 1024 * 1024:
        raise ValueError("Candidate metadata absent or exceeds its read bound")
    with info_path.open("rb") as file:
        info = plistlib.load(file)
    with (APP / "Contents/MacOS/feral-native").open("rb") as binary:
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
    return validate_identity(info, read_json(MANIFEST), digest,
                             expected_source, expected_sha)


def read_json(path, limit=1024 * 1024):
    """Bounded metadata read; never expose its contents in error messages."""
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Metadata file absent or exceeds its read bound")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Metadata must be an object")
    return value


def identity_arguments(parser):
    parser.add_argument("--expected-source", required=True, help="Actual full coordinator READY source SHA")
    parser.add_argument("--expected-sha256", required=True, help="Actual coordinator READY executable SHA256")


def profile_guard():
    if ROOT.resolve() != ROOT or not ROOT.is_dir():
        raise ValueError("The existing exact disposable profile must exist without a root symlink")
    for name in ["feral-home", "user-home", "tmp", "project"]:
        path = ROOT / name
        if path.resolve() != path or not path.is_dir():
            raise ValueError("Disposable profile contains a redirected or absent required directory")
    for name in ["launch.json", "native-exit.json", "native.log", "native-desktop.log",
                 "9-30-launch-identity.json", "acceptance-only.txt", "synthetic-attachment.txt"]:
        path = ROOT / name
        if path.resolve() != path or path.is_symlink():
            raise ValueError("Disposable launch evidence/marker must not be redirected")
    settings_path = ROOT / "feral-home/settings.json"
    if settings_path.resolve() != settings_path:
        raise ValueError("Disposable settings must not be redirected")
    settings = read_json(settings_path)
    llm = settings.get("llm", {})
    if not isinstance(llm, dict):
        raise ValueError("Malformed disposable model configuration")
    if llm.get("provider") != "ollama" or llm.get("base_url") != "http://127.0.0.1:11436/v1":
        raise ValueError("This journey requires the existing isolated loopback Ollama configuration")
    if llm.get("fallback_providers"):
        raise ValueError("Refuse an acceptance profile with fallback providers")
    for name in ["memory.db", "memory.db-wal", "memory.db-shm", "runtime.json", "data"]:
        path = ROOT / "feral-home" / name
        if path.resolve() != path or path.is_symlink():
            raise ValueError("Disposable storage/runtime metadata must not be redirected")
    previous = ROOT / "launch.json"
    if previous.exists():
        receipt = read_json(previous, 65536)
        if receipt.get("root") != str(ROOT):
            raise ValueError("Previous launch receipt has a different profile owner")
        os_pid = receipt.get("pid")
        if type(os_pid) is not int or os_pid < 1:
            raise ValueError("Invalid previous host PID")
        try:
            os.kill(os_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("Earlier recorded host PID is alive; no duplicate launch allowed")


def bounded_previous_evidence():
    """Read-only archive eligibility; no directories or evidence are written."""
    paths = [ROOT / name for name in ["launch.json", "native-exit.json", "native.log",
                                      "native-desktop.log", "9-30-launch-identity.json"]]
    for path in paths:
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 16 * 1024 * 1024)):
            raise ValueError("Prior evidence is redirected or exceeds the 16MiB per-file archive bound")
    if sum(path.stat().st_size for path in paths if path.exists()) > 20 * 1024 * 1024:
        raise ValueError("Prior evidence exceeds the 20MiB total archive bound")
    return paths


def archive_previous():
    """Bounded copies only, preserving old logs/receipts without profile reset."""
    paths = bounded_previous_evidence()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = ROOT / ("9-30-prior-" + stamp)
    archive.mkdir()
    for path in paths:
        if path.exists():
            shutil.copyfile(path, archive / path.name)
    return str(archive)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    identity_arguments(parser)
    parser.add_argument("--check-only", action="store_true", help="Read-only gates; no GUI or evidence writes")
    args = parser.parse_args()
    try:
        identity = candidate_identity(args.expected_source, args.expected_sha256)
        profile_guard()
        bounded_previous_evidence()
        if args.check_only:
            print(json.dumps({"candidate_identity": identity, "profile_guard": "passed", "launched": False}))
            return
        archive = archive_previous()
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        parser.error(str(error))
    (ROOT / "9-30-launch-identity.json").write_text(json.dumps({**identity, "root": str(ROOT),
        "candidate": str(APP), "prior_evidence": archive}, indent=2))
    # Reuse the already reviewed isolated HOME/prefs/child observer contract.
    # No --model-dir means no new Ollama process, model discovery or download.
    sys.argv = [sys.argv[0], "--root", str(ROOT), "--app", str(APP), "--hold"]
    populated_profile_probe.main()


if __name__ == "__main__":
    main()
