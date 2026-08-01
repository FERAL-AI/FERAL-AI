"""
FERAL Coding Tools
=========================
Core tools that make FERAL a real coding/system agent:
bash, read_file, write_file, edit_file, grep_search, glob_search, web_fetch.

NOTE: This is the renamed version of computer_use.py (skill_id="coding_tools").
The original computer_use.py is kept for backward compatibility.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Dict

from security.exec_mode import (
    MODE_DOCKER,
    MODE_HOST_WORKSPACE,
    MODE_REFUSED,
    NEEDS_DOCKER,
    NEEDS_WORKSPACE_GRANT,
    resolve_execution_mode,
)
from security.fetch_guard import html_to_markdown, safe_fetch
from security.sandbox_policy import SandboxPolicy
from skills.base import BaseSkill
from skills.impl import register_skill

MAX_OUTPUT = 50_000
BASH_TIMEOUT = 30
# Reference-codebase ergonomics (AUDIT-r14 round3 engine spec #4):
# default grep to file names, paginate, relativize paths to keep tool
# output compact for the LLM context window.
GREP_DEFAULT_HEAD_LIMIT = 250
GLOB_DEFAULT_HEAD_LIMIT = 100
DANGEROUS_COMMANDS = re.compile(
    r"\b(rm\s+-rf\s+/|mkfs|dd\s+if=|:(){ :|fork\s*bomb|shutdown|reboot|halt|poweroff)\b",
    re.IGNORECASE,
)


def _workspace_root() -> Path:
    """Workspace root for relativizing tool output paths."""
    return Path(os.environ.get("FERAL_WORKSPACE", "") or Path.cwd()).expanduser()


def _relativize(p: "str | Path") -> str:
    """Render a path relative to the workspace root when it's inside it,
    otherwise return it unchanged. Mirrors the reference tool output
    (`toRelativePath`) so the model sees `skills/foo.py` not a 90-char
    absolute path."""
    try:
        ap = Path(p).expanduser().resolve()
        return str(ap.relative_to(_workspace_root().resolve()))
    except (ValueError, OSError):
        return str(p)


def _paginate(lines: "list[str]", head_limit: int, offset: int) -> "tuple[list[str], bool]":
    """Apply offset + head_limit (0 = unlimited). Returns (window, truncated)."""
    total = len(lines)
    if head_limit == 0:
        window = lines[offset:]
    else:
        window = lines[offset: offset + head_limit]
    truncated = head_limit != 0 and (offset + len(window)) < total
    return window, truncated


def _check_shell_quotes(command: str) -> str | None:
    """Return an error string if shell quotes are unbalanced, else None."""
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == '\\' and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        i += 1
    if in_single or in_double:
        return (
            "Shell syntax error: unbalanced quotes in command. "
            "Tip: use coding_tools__write_file to create files with "
            "arbitrary content instead of shell echo/printf."
        )
    return None


@register_skill
class CodingToolsSkill(BaseSkill):
    def __init__(self):
        super().__init__(skill_id="coding_tools")
        self._sandbox_bash_enabled = os.getenv("FERAL_SANDBOX_BASH", "false").lower() in ("true", "1", "yes")
        self._policy: SandboxPolicy | None = None

    def _resolve_docker_sandbox(self):
        """Look up Docker per-call so a daemon that comes up after FERAL
        boot is still reachable. Mirrors ComputerUseSkill so the two
        bash-bearing skills behave identically.
        """
        try:
            from security.docker_sandbox import get_sandbox
        except Exception:
            return None
        try:
            sandbox = get_sandbox()
        except Exception:
            return None
        if sandbox is None:
            return None
        try:
            available_attr = getattr(sandbox, "available", None)
            available = (
                bool(available_attr())
                if callable(available_attr)
                else bool(available_attr)
            )
        except Exception:
            available = False
        return sandbox if available else None

    def _get_policy(self) -> SandboxPolicy:
        if self._policy is None:
            self._policy = SandboxPolicy.load_default()
        return self._policy

    def _check_read(self, path_str: str) -> dict | None:
        policy = self._get_policy()
        if not policy.can_read_path(path_str):
            return {
                "success": False, "status_code": 403,
                "data": {"permission_needed": True, "path": path_str, "operation": "read"},
                "error": f"Permission denied: no read access to {path_str}. Grant access first.",
            }
        return None

    def _check_write(self, path_str: str) -> dict | None:
        policy = self._get_policy()
        if not policy.can_write_path(path_str):
            return {
                "success": False, "status_code": 403,
                "data": {"permission_needed": True, "path": path_str, "operation": "write"},
                "error": f"Permission denied: no write access to {path_str}. Grant access first.",
            }
        return None

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        dispatch = {
            "bash": self._bash,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "grep_search": self._grep_search,
            "glob_search": self._glob_search,
            "web_fetch": self._web_fetch,
            "index_folder": self._index_folder,
        }
        handler = dispatch.get(endpoint_id)
        if not handler:
            return {"success": False, "status_code": 404, "data": None, "error": f"Unknown endpoint: {endpoint_id}"}
        try:
            return await handler(args)
        except Exception as e:
            return {"success": False, "status_code": 500, "data": None, "error": str(e)}

    # ── bash ──────────────────────────────────────────────────────

    async def _bash(self, args: dict) -> dict:
        """Run a shell command in whichever execution mode is authorised.

        The pre-fix version had two outcomes: Docker, or a 503. Since the
        manifest declared ``requires_sandbox: true`` and Docker is absent on
        a default macOS install, the advertised developer shell never ran.
        The mode is now decided by ``security.exec_mode.resolve_execution_mode``
        from (command, resolved cwd, autonomy mode, grant state); see that
        module for the full table.
        """
        command = args.get("command", "")
        if not command:
            return {"success": False, "status_code": 400, "data": None, "error": "No command provided"}

        if DANGEROUS_COMMANDS.search(command):
            return {
                "success": False, "status_code": 403, "data": None,
                "error": f"Blocked potentially destructive command: {command}",
            }

        quote_err = _check_shell_quotes(command)
        if quote_err:
            return {"success": False, "status_code": 400, "data": None, "error": quote_err}

        timeout = min(int(args.get("timeout", BASH_TIMEOUT)), 120)

        sandbox_required = bool(args.get("_feral_require_sandbox"))
        # Probe Docker only when a mode that needs it is in play, so the
        # common host-workspace path does not pay for a `docker info` call.
        docker_sandbox = self._resolve_docker_sandbox() if (
            sandbox_required or self._sandbox_bash_enabled
        ) else None

        decision = resolve_execution_mode(
            command,
            policy=self._get_policy(),
            cwd=args.get("cwd"),
            skill_id=self.skill_id,
            requires_sandbox=sandbox_required,
            prefer_sandbox=self._sandbox_bash_enabled,
            docker_available=docker_sandbox is not None,
        )

        if decision.mode == MODE_DOCKER:
            original_timeout = getattr(docker_sandbox, "_timeout", BASH_TIMEOUT)
            try:
                docker_sandbox._timeout = timeout
                result = await docker_sandbox.execute_shell(command)
            finally:
                docker_sandbox._timeout = original_timeout

            stdout = (result.get("stdout") or "")[:MAX_OUTPUT]
            stderr = (result.get("stderr") or "")[:MAX_OUTPUT]
            exit_code = int(result.get("exit_code", -1))
            success = bool(result.get("success"))
            data = {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "execution_time_ms": result.get("execution_time_ms"),
                "sandbox": "docker",
                "execution_mode": MODE_DOCKER,
            }
            return {
                "success": success,
                "status_code": 200,
                "data": data,
                "error": stderr if not success else None,
            }

        if decision.mode == MODE_REFUSED:
            return self._refuse_bash(decision)

        sandbox_note = None
        if self._sandbox_bash_enabled and docker_sandbox is None:
            sandbox_note = "FERAL_SANDBOX_BASH is enabled but Docker sandbox is unavailable; executed on host."

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=decision.cwd,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"success": False, "status_code": 408, "data": None, "error": f"Command timed out after {timeout}s"}

        stdout = stdout_b.decode(errors="replace")[:MAX_OUTPUT]
        stderr = stderr_b.decode(errors="replace")[:MAX_OUTPUT]

        return {
            "success": proc.returncode == 0,
            "status_code": 200,
            "data": {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": proc.returncode,
                "sandbox": "host",
                "execution_mode": MODE_HOST_WORKSPACE,
                "cwd": decision.cwd,
                "workspace": decision.workspace,
                "workspace_source": decision.workspace_source,
                "note": sandbox_note,
            },
            "error": stderr if proc.returncode != 0 else None,
        }

    @staticmethod
    def _refuse_bash(decision) -> dict:
        """Turn a refusal into something the operator can act on.

        A missing grant reuses the ``permission_needed`` contract the file
        tools already speak, so ``ToolRunner`` raises the same Allow/Deny
        folder card it raises for ``write_file``. A missing sandbox keeps
        the Docker setup step. Either way the response names the mode that
        was required instead of a bare 503.
        """
        data = {
            "execution_mode": MODE_REFUSED,
            "needs": decision.needs,
            "permission_needed": decision.needs == NEEDS_WORKSPACE_GRANT,
        }
        if decision.needs == NEEDS_WORKSPACE_GRANT:
            data["path"] = decision.denied_path or decision.cwd
            data["operation"] = "read"
            status = 403
        elif decision.needs == NEEDS_DOCKER:
            data["sandbox"] = "unavailable"
            data["setup_step"] = (
                "Start Docker Desktop (or set up an alternative sandbox) so "
                "generated code can run isolated. FERAL refuses to run "
                "generated code on the host even inside a granted workspace."
            )
            status = 503
        else:
            status = 403
        return {
            "success": False,
            "status_code": status,
            "data": data,
            "error": decision.reason,
        }

    # ── read_file ─────────────────────────────────────────────────

    async def _read_file(self, args: dict) -> dict:
        path = Path(args.get("path", "")).expanduser()
        denied = self._check_read(str(path))
        if denied:
            return denied

        def _stat_and_read() -> tuple[dict | None, str]:
            """Blocking stat + read. Returns ``(error_envelope, text)``.

            Kept as one thread hop so the stat checks and the read cannot
            interleave with other coroutines against a changing file.
            """
            if not path.exists():
                return {"success": False, "status_code": 404, "data": None, "error": f"File not found: {path}"}, ""
            if not path.is_file():
                return {"success": False, "status_code": 400, "data": None, "error": f"Not a file: {path}"}, ""
            if path.stat().st_size > 2_000_000:
                return {"success": False, "status_code": 413, "data": None, "error": "File too large (>2MB). Use offset/limit."}, ""
            return None, path.read_text(errors="replace")

        error, text = await asyncio.to_thread(_stat_and_read)
        if error is not None:
            return error

        lines = text.splitlines()

        offset = int(args.get("offset", 1)) - 1
        limit = int(args.get("limit", 0)) or len(lines)
        selected = lines[max(0, offset):offset + limit]

        numbered = "\n".join(f"{i + offset + 1:>6}|{line}" for i, line in enumerate(selected))

        return {
            "success": True,
            "status_code": 200,
            "data": {"path": str(path), "content": numbered, "total_lines": len(lines)},
            "error": None,
        }

    # ── write_file ────────────────────────────────────────────────

    async def _write_file(self, args: dict) -> dict:
        path = Path(args.get("path", "")).expanduser()
        content = args.get("content", "")
        if not str(path):
            return {"success": False, "status_code": 400, "data": None, "error": "No path provided"}
        denied = self._check_write(str(path))
        if denied:
            return denied

        def _mkdir_and_write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        await asyncio.to_thread(_mkdir_and_write)

        return {
            "success": True,
            "status_code": 200,
            "data": {"path": str(path), "bytes_written": len(content.encode())},
            "error": None,
        }

    # ── edit_file ─────────────────────────────────────────────────

    async def _edit_file(self, args: dict) -> dict:
        path = Path(args.get("path", "")).expanduser()
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")

        denied = self._check_write(str(path))
        if denied:
            return denied

        def _read_match_write() -> dict | None:
            """Blocking read-modify-write. Returns an error envelope or None.

            Kept as one thread hop so the uniqueness check and the write see
            the same file contents.
            """
            if not path.exists():
                return {"success": False, "status_code": 404, "data": None, "error": f"File not found: {path}"}
            if not old_text:
                return {"success": False, "status_code": 400, "data": None, "error": "old_text is required"}

            content = path.read_text(errors="replace")
            count = content.count(old_text)
            if count == 0:
                return {"success": False, "status_code": 404, "data": None, "error": "old_text not found in file"}
            if count > 1:
                return {"success": False, "status_code": 409, "data": None, "error": f"old_text matches {count} locations, provide more context to be unique"}

            path.write_text(content.replace(old_text, new_text, 1))
            return None

        error = await asyncio.to_thread(_read_match_write)
        if error is not None:
            return error

        return {
            "success": True,
            "status_code": 200,
            "data": {"path": str(path), "replacements": 1},
            "error": None,
        }

    # ── grep_search ───────────────────────────────────────────────

    async def _grep_search(self, args: dict) -> dict:
        pattern = args.get("pattern", "")
        search_path = args.get("path", ".")
        include = args.get("include", "")
        # Default to file names (cheap, narrows fast); the model asks for
        # `content` only after it knows which files matter.
        output_mode = (args.get("output_mode") or "files_with_matches").lower()
        try:
            head_limit = int(args.get("head_limit", GREP_DEFAULT_HEAD_LIMIT))
        except (TypeError, ValueError):
            head_limit = GREP_DEFAULT_HEAD_LIMIT
        try:
            offset = int(args.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0

        if not pattern:
            return {"success": False, "status_code": 400, "data": None, "error": "No search pattern"}
        denied = self._check_read(search_path)
        if denied:
            return denied

        cmd = ["rg", "--color=never"]
        if output_mode == "files_with_matches":
            cmd += ["--files-with-matches"]
        elif output_mode == "count":
            cmd += ["--count"]
        else:  # content
            output_mode = "content"
            cmd += ["--line-number", "--no-heading"]
        if include:
            cmd += ["--glob", include]
        cmd += ["--", pattern, search_path]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=15)
        except FileNotFoundError:
            return await self._grep_fallback(pattern, search_path, include, output_mode, head_limit, offset)
        except asyncio.TimeoutError:
            return {"success": False, "status_code": 408, "data": None, "error": "Search timed out"}

        stdout = stdout_b.decode(errors="replace")[:MAX_OUTPUT]
        raw_lines = stdout.strip().splitlines() if stdout.strip() else []
        total = len(raw_lines)
        window, truncated = _paginate(raw_lines, head_limit, offset)

        if output_mode == "files_with_matches":
            data: Dict[str, Any] = {
                "mode": "files_with_matches",
                "files": [_relativize(line) for line in window],
                "total_files": total,
            }
        elif output_mode == "count":
            counts = []
            for line in window:
                f, sep, c = line.rpartition(":")
                counts.append({"file": _relativize(f) if sep else line, "count": c})
            data = {"mode": "count", "counts": counts, "total_files": total}
        else:
            matches = []
            for line in window:
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    matches.append({"file": _relativize(parts[0]), "line": parts[1], "text": parts[2]})
                else:
                    matches.append({"text": line})
            data = {"mode": "content", "matches": matches, "total": total}

        if truncated:
            data["truncated"] = True
            data["pagination"] = {"limit": head_limit, "offset": offset, "next_offset": offset + len(window)}

        return {"success": True, "status_code": 200, "data": data, "error": None}

    async def _grep_fallback(
        self,
        pattern: str,
        search_path: str,
        include: str,
        output_mode: str = "files_with_matches",
        head_limit: int = GREP_DEFAULT_HEAD_LIMIT,
        offset: int = 0,
    ) -> dict:
        """Pure-Python fallback when ripgrep is not installed. Honors the
        same output_mode + pagination contract as the rg path."""
        regex = re.compile(pattern)
        root = Path(search_path).expanduser()
        glob_pat = include or "**/*"
        content_rows: list[dict] = []
        files_with: list[str] = []
        # Generous scan cap so pagination has something to page over.
        scan_cap = (offset + head_limit) * 4 if head_limit else 5000

        # ``search_path`` may name a single FILE rather than a directory.
        # ripgrep accepts that and searches it, but ``Path(file).glob(...)``
        # yields nothing, so the fallback silently returned zero matches for
        # every single-file grep on any machine without ripgrep installed.
        # The rg path masked it everywhere rg happens to be present.
        candidates = [root] if root.is_file() else root.glob(glob_pat)

        for fp in candidates:
            if not fp.is_file() or fp.stat().st_size > 1_000_000:
                continue
            try:
                hit_in_file = False
                for i, line in enumerate(fp.read_text(errors="replace").splitlines(), 1):
                    if regex.search(line):
                        hit_in_file = True
                        content_rows.append({"file": _relativize(fp), "line": str(i), "text": line.strip()})
                        if len(content_rows) >= scan_cap:
                            break
                if hit_in_file:
                    files_with.append(_relativize(fp))
            except (PermissionError, OSError):
                continue
            if len(content_rows) >= scan_cap:
                break

        if output_mode == "files_with_matches":
            window, truncated = _paginate(files_with, head_limit, offset)
            data: Dict[str, Any] = {"mode": "files_with_matches", "files": window, "total_files": len(files_with)}
        elif output_mode == "count":
            window, truncated = _paginate(files_with, head_limit, offset)
            per = {}
            for r in content_rows:
                per[r["file"]] = per.get(r["file"], 0) + 1
            data = {"mode": "count", "counts": [{"file": f, "count": str(per.get(f, 0))} for f in window], "total_files": len(files_with)}
        else:
            window, truncated = _paginate(content_rows, head_limit, offset)
            data = {"mode": "content", "matches": window, "total": len(content_rows)}

        if truncated:
            data["truncated"] = True
            data["pagination"] = {"limit": head_limit, "offset": offset, "next_offset": offset + len(window)}

        return {"success": True, "status_code": 200, "data": data, "error": None}

    # ── glob_search ───────────────────────────────────────────────

    async def _glob_search(self, args: dict) -> dict:
        pattern = args.get("pattern", "")
        root = Path(args.get("path", ".")).expanduser()
        try:
            head_limit = int(args.get("head_limit", GLOB_DEFAULT_HEAD_LIMIT))
        except (TypeError, ValueError):
            head_limit = GLOB_DEFAULT_HEAD_LIMIT

        if not pattern:
            return {"success": False, "status_code": 400, "data": None, "error": "No glob pattern"}
        denied = self._check_read(str(root))
        if denied:
            return denied

        # Prefer ripgrep: respects .gitignore, sorts by mtime (most
        # relevant first), much faster than Path.glob on large trees.
        cmd = ["rg", "--files", "--color=never", "--sort=modified", "--glob", pattern, str(root)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, _stderr_b = await asyncio.wait_for(proc.communicate(), timeout=15)
        except FileNotFoundError:
            # Pure-Python fallback when ripgrep is absent.
            files = []
            for fp in root.glob(pattern):
                files.append(_relativize(fp))
                if head_limit and len(files) >= head_limit:
                    break
            data: Dict[str, Any] = {"files": files, "total": len(files)}
            if head_limit and len(files) >= head_limit:
                data["truncated"] = True
            return {"success": True, "status_code": 200, "data": data, "error": None}
        except asyncio.TimeoutError:
            return {"success": False, "status_code": 408, "data": None, "error": "Glob timed out"}

        all_files = stdout_b.decode(errors="replace").strip().splitlines()
        all_files = [f for f in all_files if f]
        total = len(all_files)
        window = all_files if head_limit == 0 else all_files[:head_limit]
        data = {"files": [_relativize(f) for f in window], "total": total}
        if head_limit and total > head_limit:
            data["truncated"] = True
            data["pagination"] = {"limit": head_limit, "shown": len(window)}

        return {"success": True, "status_code": 200, "data": data, "error": None}

    # ── web_fetch ─────────────────────────────────────────────────

    async def _web_fetch(self, args: dict) -> dict:
        url = args.get("url", "")
        max_length = int(args.get("max_length", 10_000))

        if not url:
            return {"success": False, "status_code": 400, "data": None, "error": "No URL provided"}

        result = await safe_fetch(url, timeout=15.0)
        if not result["success"]:
            code = int(result.get("status_code") or 400)
            err = result.get("error") or "fetch failed"
            return {"success": False, "status_code": code if code else 400, "data": None, "error": err}

        text = result["content"]
        content_type = result.get("content_type", "")
        if "html" in content_type.lower():
            text = html_to_markdown(text)

        return {
            "success": True,
            "status_code": 200,
            "data": {"url": url, "content": text[:max_length], "length": len(text)},
            "error": None,
        }

    # ── index_folder ──────────────────────────────────────────────

    _GITIGNORE_DIRS = frozenset({
        ".git", "__pycache__", "node_modules", ".tox", ".mypy_cache",
        ".pytest_cache", "dist", "build", ".next", ".nuxt", "venv", ".venv",
    })
    _MAX_INDEX_FILES = 500
    _MAX_INDEX_BYTES = 50 * 1024 * 1024

    async def _index_folder(self, args: dict) -> dict:
        root = Path(args.get("path", "")).expanduser().resolve()
        if not root.is_dir():
            return {"success": False, "status_code": 404, "data": None, "error": f"Not a directory: {root}"}
        denied = self._check_read(str(root))
        if denied:
            return denied

        tree_lines: list[str] = []
        summaries: list[str] = []
        total_bytes = 0
        file_count = 0

        gitignore_patterns: list[str] = []
        gi_path = root / ".gitignore"
        if gi_path.is_file():
            for line in gi_path.read_text(errors="replace").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    gitignore_patterns.append(stripped)

        def _should_skip(p: Path) -> bool:
            if p.name.startswith(".") and p.is_dir() and p.name != ".github":
                return True
            if p.name in self._GITIGNORE_DIRS:
                return True
            for pat in gitignore_patterns:
                try:
                    if p.match(pat):
                        return True
                except ValueError:
                    pass
            return False

        for dirpath, dirnames, filenames in os.walk(root):
            dp = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not _should_skip(dp / d)]
            rel = dp.relative_to(root)
            indent = "  " * len(rel.parts)

            for fname in sorted(filenames):
                if file_count >= self._MAX_INDEX_FILES or total_bytes >= self._MAX_INDEX_BYTES:
                    break
                fp = dp / fname
                if _should_skip(fp) or not fp.is_file():
                    continue
                fsize = fp.stat().st_size
                total_bytes += fsize
                file_count += 1
                tree_lines.append(f"{indent}{fname} ({fsize:,}B)")

                if fsize < 8192 and fsize > 0:
                    ext = fp.suffix.lower()
                    if ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".swift", ".rs", ".go",
                               ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".cfg", ".ini",
                               ".html", ".css", ".sh", ".sql"):
                        try:
                            head = fp.read_text(errors="replace")[:500]
                            summaries.append(f"--- {rel / fname} ---\n{head}")
                        except (PermissionError, OSError):
                            pass

            if file_count >= self._MAX_INDEX_FILES or total_bytes >= self._MAX_INDEX_BYTES:
                tree_lines.append("... (truncated)")
                break

        tree_text = f"Folder: {root}\nFiles: {file_count} | Size: {total_bytes:,}B\n\n" + "\n".join(tree_lines)
        summary_text = "\n\n".join(summaries[:60])

        return {
            "success": True,
            "status_code": 200,
            "data": {
                "path": str(root),
                "file_count": file_count,
                "total_bytes": total_bytes,
                "tree": tree_text,
                "file_previews": summary_text[:30_000],
            },
            "error": None,
        }
