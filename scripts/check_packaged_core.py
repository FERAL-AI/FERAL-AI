"""Read-only comparison of packaged production Python against a frozen Git commit.

This checks the runtime payload only. It does not prove how the native executable
was compiled, authenticate a release publisher, or establish signing acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

MAX_TREE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SOURCES = 10_000
EXCLUDED = {"tests", "build", "dist", "__pycache__"}


def production_source(path: str) -> bool:
    parts = Path(path).parts
    return (len(parts) > 1 and parts[0] == "feral-core" and path.endswith(".py")
            and not EXCLUDED.intersection(parts[1:-1]))


def compare_payload(bundle: Path, repo: Path, revision: str) -> dict:
    result = {"ok": False, "source_revision": revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None,
              "checked_files": 0, "issues": [], "scope": "Packaged production Python source equality only"}

    def issue(code, path=""):
        if len(result["issues"]) < 100:
            result["issues"].append({"code": code, "path": path})

    if result["source_revision"] is None:
        issue("full_commit_sha_required")
        return result
    try:
        # Disable ambient Git overrides and optional writes, as in the recovery
        # helper. No network, content checkout, hooks or shell evaluation.
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        identity = subprocess.run(["git", "-C", str(repo), "cat-file", "-t", revision], capture_output=True, timeout=10, env=env)
        if identity.returncode or identity.stdout.strip() != b"commit":
            issue("source_commit_unavailable")
            return result
        command = ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "ls-tree", "-rz", revision, "--", "feral-core"]
        tree = subprocess.run(command, capture_output=True, timeout=20, env=env)
        if tree.returncode or len(tree.stdout) > MAX_TREE_BYTES:
            issue("source_tree_unavailable_or_over_budget")
            return result
        expected = {}
        for entry in tree.stdout.split(b"\0"):
            if not entry:
                continue
            header, name = entry.split(b"\t", 1)
            path = name.decode("utf-8")
            mode, kind, oid = header.decode("ascii").split()
            if production_source(path):
                if kind != "blob" or mode not in {"100644", "100755"} or not re.fullmatch(r"[0-9a-f]{40}", oid):
                    issue("unsupported_source_entry", path)
                    continue
                expected[path] = oid
        if not expected or len(expected) > MAX_SOURCES:
            issue("source_inventory_empty_or_over_budget")
            return result
        app_root = bundle.resolve(strict=True)
        core = app_root / "Contents/Resources/feral-core"
        if not core.is_dir() or core.is_symlink() or app_root not in core.resolve().parents:
            issue("packaged_core_missing_or_external")
            return result
        for path, oid in expected.items():
            packaged = core / Path(path).relative_to("feral-core")
            if not packaged.is_file():
                issue("source_missing", path)
                continue
            resolved = packaged.resolve(strict=True)
            if packaged.is_symlink() or core.resolve() not in resolved.parents:
                issue("source_external_or_symlink", path)
                continue
            with packaged.open("rb") as handle:
                content = handle.read(MAX_SOURCE_BYTES + 1)
            if len(content) > MAX_SOURCE_BYTES:
                issue("source_file_over_budget", path)
                continue
            git_blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
            if hashlib.sha1(git_blob).hexdigest() != oid:
                issue("source_content_mismatch", path)
            result["checked_files"] += 1
        actual = 0
        for directory, dirs, files in os.walk(core, followlinks=False):
            dirs[:] = [name for name in dirs if name not in EXCLUDED]
            for name in files:
                if not name.endswith(".py"):
                    continue
                actual += 1
                if actual > MAX_SOURCES:
                    issue("packaged_inventory_over_budget")
                    break
                path = "feral-core/" + (Path(directory) / name).relative_to(core).as_posix()
                if path not in expected:
                    issue("unexpected_production_source", path)
            if actual > MAX_SOURCES:
                break
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        issue("comparison_failed_private_details_withheld")
    result["ok"] = not result["issues"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--revision", required=True, help="Full frozen 40-character Git commit SHA")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = compare_payload(args.bundle, args.repo, args.revision)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
