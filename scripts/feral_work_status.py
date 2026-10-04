#!/usr/bin/env python3
"""Read-only checkout and recovery-document status, without private runtime access."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit


DOCUMENTS = (
    "AGENTS.md",
    "codex.md",
    "docs/roadmap/theora-personal-agent/EXECUTION_PLAN.md",
    "docs/roadmap/theora-personal-agent/WORK_STATE.md",
    "docs/roadmap/theora-personal-agent/RESUME_WORK.md",
)
EXPECTED_ORIGIN = "github.com/feral-ai/feral-ai"


class StatusError(RuntimeError):
    """A bounded, public diagnostic that does not expose subprocess output."""


def git(repo: Path, *arguments: str) -> str:
    # Ignore ambient repository overrides and avoid optional index writes.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo), *arguments],
            env=environment,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StatusError("Git is unavailable or did not answer within 15 seconds.") from exc
    if result.returncode:
        raise StatusError(f"Git could not complete {arguments[0]} in the selected checkout.")
    return result.stdout.decode("utf-8", errors="surrogateescape")


def remote_identity(remote: str) -> str:
    """Compare the public origin identity without copying credentials into errors."""
    if "://" in remote:
        parsed = urlsplit(remote)
        host, path = parsed.hostname or "", parsed.path
    else:
        match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", remote)
        if not match:
            return ""
        host, path = match.groups()
    return f"{host}/{path.strip('/').removesuffix('.git')}".lower()


def redact_remote(remote: str) -> str:
    if "://" in remote:
        parsed = urlsplit(remote)
        host = parsed.hostname or ""
        netloc = f"[redacted]@{host}" if parsed.username is not None else host
        # Do not retain query strings, fragments, passwords or nonstandard ports.
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    match = re.fullmatch(r"(?:([^/@:]+)@)?([^/:]+):(.+)", remote)
    if match:
        user, host, path = match.groups()
        prefix = "git@" if user == "git" else "[redacted]@" if user else ""
        return f"{prefix}{host}:{path}"
    return "[unrecognized remote]"


def dirty_paths(porcelain: str) -> list[dict[str, str]]:
    """Parse Git's NUL-delimited format, including both paths in a rename."""
    entries = iter(porcelain.split("\0"))
    paths = []
    for entry in entries:
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise StatusError("Git returned an invalid working-tree status record.")
        status = entry[:2]
        item = {"status": status, "path": entry[3:]}
        if "R" in status or "C" in status:
            original = next(entries, "")
            if not original:
                raise StatusError("Git returned an incomplete rename status record.")
            item["original_path"] = original
        paths.append(item)
    return paths


def collect_status(repo: Path) -> dict:
    repo = repo.resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise StatusError("Select the FERAL checkout itself; no local .git marker was found.")
    actual = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    if actual != repo:
        raise StatusError("Git resolved another checkout; refusing to inspect an enclosing repository.")
    for marker in ("feral-core/api/server.py", "feral-core/pyproject.toml"):
        if not (repo / marker).is_file():
            raise StatusError("The selected checkout is missing the expected FERAL source layout.")
    remote = git(repo, "remote", "get-url", "origin").strip()
    try:
        identity = remote_identity(remote)
    except ValueError as exc:
        raise StatusError("The origin URL is malformed.") from exc
    if identity != EXPECTED_ORIGIN:
        raise StatusError("Origin must identify github.com/FERAL-AI/FERAL-AI; refusing another repository.")
    status = {
        "ok": True,
        "repo": str(repo),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "head": git(repo, "rev-parse", "HEAD").strip(),
        "origin": redact_remote(remote),
        "dirty_paths": dirty_paths(git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=normal")),
        "documents": {name: (repo / name).is_file() for name in DOCUMENTS},
        "warnings": [],
    }
    try:
        status["disk_free_bytes"] = shutil.disk_usage(repo).free
    except OSError:
        status["disk_free_bytes"] = None
        status["warnings"].append("Free disk space could not be read; verify it before a build.")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)
    try:
        status = collect_status(args.repo)
    except (StatusError, OSError) as exc:
        # OS errors can carry private paths; only our bounded diagnostics are public.
        message = str(exc) if isinstance(exc, StatusError) else "The checkout could not be inspected."
        status = {"ok": False, "error": message}
    if args.json:
        print(json.dumps(status, indent=2))
    elif not status["ok"]:
        print(f"FERAL status: {status['error']}")
    else:
        print(f"FERAL checkout: {json.dumps(status['repo'])}")
        print(f"Branch: {json.dumps(status['branch'])} | HEAD: {status['head']}")
        print(f"Origin: {json.dumps(status['origin'])}")
        free = status["disk_free_bytes"]
        print(f"Free disk: {free / 1024**3:.1f} GiB" if free is not None else "Free disk: unknown")
        print(f"Dirty paths: {len(status['dirty_paths'])}")
        for item in status["dirty_paths"]:
            original = f" <- {json.dumps(item['original_path'])}" if "original_path" in item else ""
            print(f"  {item['status']} {json.dumps(item['path'])}{original}")
        missing = [name for name, exists in status["documents"].items() if not exists]
        print("Recovery documents: present" if not missing else "Missing recovery documents:")
        for name in missing:
            print(f"  {name}")
        for warning in status["warnings"]:
            print(f"Warning: {warning}")
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
