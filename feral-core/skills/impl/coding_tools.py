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
import dataclasses
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from security.exec_mode import (
    MODE_DOCKER,
    MODE_HOST_WORKSPACE,
    MODE_REFUSED,
    NEEDS_DOCKER,
    NEEDS_WORKSPACE_GRANT,
    resolve_execution_mode,
)
from security.command_unwrap import scannable_command
from security.fetch_guard import html_to_markdown, safe_fetch
from security.safe_regex import UnsafePatternError, compile_safe_regex
from security.sandbox_policy import SandboxPolicy
from skills import checkpoints as checkpoint_store
from skills import diagnostics as diagnostics_mod
from skills import edit_matchers
from skills import file_state
from skills.base import BaseSkill
from skills.call_context import ToolCallContext, require_context
from skills.impl import register_skill

logger = logging.getLogger("feral.skills.coding_tools")

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
            "revert_turn": self._revert_turn,
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

        # Scan the unwrapped form as well as the raw one. This pattern set
        # reads the literal string, so `echo cm0gLXJmIC8K | base64 -d | sh`
        # sails past every entry in it while meaning exactly what the
        # entries exist to stop. exec_mode applies the same normalisation
        # immediately after, but this check runs first and returns first.
        if DANGEROUS_COMMANDS.search(command) or DANGEROUS_COMMANDS.search(
            scannable_command(command)
        ):
            return {
                "success": False, "status_code": 403, "data": None,
                "error": f"Blocked potentially destructive command: {command}",
            }

        quote_err = _check_shell_quotes(command)
        if quote_err:
            return {"success": False, "status_code": 400, "data": None, "error": quote_err}

        timeout = min(int(args.get("timeout", BASH_TIMEOUT)), 120)

        # A shell command can rewrite any file on the machine, and working
        # out which ones from the command text is not decidable for a
        # shell. So unless every segment is a known read-only tool, drop
        # the whole session's file observations rather than guess at
        # paths. Invalidated up front: the command may write and then
        # fail, and a non-zero exit is no evidence that nothing changed.
        if not file_state.bash_is_read_only(command):
            ctx = require_context("coding_tools__bash")
            file_state.get_tracker().invalidate_session(
                ctx.session_id, reason="bash command was not provably read-only",
            )

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

        def _stat_read_and_observe() -> "tuple[dict | None, str, object]":
            """Blocking permission check + stat + read + fingerprint.

            Kept as one thread hop so the stat checks and the read cannot
            interleave with other coroutines against a changing file. The
            fingerprint joins the same hop rather than taking one of its
            own, because an observation recorded from a second hop could
            describe a version of the file the agent never saw, which
            would make a later staleness check pass when it should fail.

            The grant check is in here too, and that is not cosmetic:
            ``SandboxPolicy.can_read_path`` calls ``_load_grants()``,
            which does ``read_text`` on
            ``~/.feral/workspace_grants.json`` every single call. It was
            running on the event loop. Wave 1's thread-identity spy could
            not see it, because it records one thread per patched method
            name and the in-hop ``read_text`` overwrote the loop-thread
            record a moment later.
            """
            denied = self._check_read(str(path))
            if denied:
                return denied, "", None
            if not path.exists():
                return {"success": False, "status_code": 404, "data": None, "error": f"File not found: {path}"}, "", None
            if not path.is_file():
                return {"success": False, "status_code": 400, "data": None, "error": f"Not a file: {path}"}, "", None
            if path.stat().st_size > 2_000_000:
                return {"success": False, "status_code": 413, "data": None, "error": "File too large (>2MB). Use offset/limit."}, "", None
            text = path.read_text(errors="replace")
            return None, text, file_state.get_tracker().observe(path)

        error, text, observation = await asyncio.to_thread(_stat_read_and_observe)
        if error is not None:
            return error

        lines = text.splitlines()

        offset = int(args.get("offset", 1)) - 1
        limit = int(args.get("limit", 0)) or len(lines)
        selected = lines[max(0, offset):offset + limit]

        numbered = "\n".join(f"{i + offset + 1:>6}|{line}" for i, line in enumerate(selected))

        # Record what the agent actually looked at, so a later write to
        # this path can tell "edited against what it read" from "edited
        # from memory" and from "edited against a version that has since
        # changed". See skills/file_state.py. Storing is a dict write, so
        # it stays on the loop; the blocking fingerprint already happened
        # above, inside the same hop as the read it describes.
        ctx = require_context("coding_tools__read_file")
        if observation is not None:
            partial = len(selected) < len(lines)
            file_state.get_tracker().remember(
                ctx.session_id,
                dataclasses.replace(
                    observation,
                    partial=partial,
                    window=(offset + 1, offset + len(selected)) if partial else None,
                ),
            )

        return {
            "success": True,
            "status_code": 200,
            "data": {"path": str(path), "content": numbered, "total_lines": len(lines)},
            "error": None,
        }

    # ── shared write plumbing ─────────────────────────────────────

    @staticmethod
    def _read_verbatim(path: Path) -> str:
        """Read without universal-newline translation.

        ``Path.read_text`` turns CRLF into LF in the returned string, so
        the edit matcher would never see the file's real line endings and
        ``write_text`` would then persist LF. That combination silently
        converts a CRLF file to LF on the first edit, after which every
        exact match against it fails for reasons nothing in the tool
        output explains.
        """
        with path.open("r", errors="replace", newline="") as fh:
            return fh.read()

    @staticmethod
    def _write_verbatim(path: Path, content: str) -> None:
        """Write without newline translation, so the bytes on disk are
        exactly the string we computed.

        ``Path.write_text`` rather than ``path.open(...).write`` on
        purpose: it takes ``newline`` from Python 3.10, and staying on the
        same method Wave 1's thread-identity spy patches keeps that
        regression guard pointed at the real write. (``Path.read_text``
        only gained ``newline`` in 3.13, which is why the read side of
        this pair cannot do the same.)
        """
        path.write_text(content, newline="")

    @staticmethod
    def _edit_limits() -> tuple[int, int]:
        """Cost guard for the fallback matchers. The sliding-window
        strategies are O(file_lines x needle_lines), so above these sizes
        only the exact matcher runs."""
        def _int(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, str(default))))
            except ValueError:
                return default

        return (
            _int("FERAL_EDIT_MAX_CONTENT_LINES", edit_matchers.DEFAULT_MAX_CONTENT_LINES),
            _int("FERAL_EDIT_MAX_NEEDLE_LINES", edit_matchers.DEFAULT_MAX_NEEDLE_LINES),
        )

    @staticmethod
    def _guard_write(ctx: "ToolCallContext", path: Path) -> "tuple[dict | None, dict | None]":
        """Run the read-before-edit / staleness check.

        Returns ``(refusal, warning)``. In the default ``warn`` mode the
        refusal is always ``None`` and the caller folds the warning into a
        successful result, which is what gives us telemetry on how often
        the guard would fire before it starts failing real work.
        """
        check = file_state.get_tracker().check_write(ctx.session_id, path)
        if check.verdict == file_state.VERDICT_OK:
            return None, None
        if check.allowed:
            return None, check.as_dict()
        return {
            "success": False,
            "status_code": 409,
            "data": {"read_before_edit": check.as_dict(), "path": str(path)},
            "error": check.message,
        }, None

    @staticmethod
    def _capture_checkpoint(ctx: "ToolCallContext", path: Path) -> Optional[str]:
        """Stash the pre-write bytes. Never raises, never blocks the write.

        A checkpoint that fails is a lost undo. A write that fails because
        the undo could not be recorded is a broken agent, so every failure
        here is logged and swallowed.
        """
        if not ctx.turn_id:
            return None
        try:
            return checkpoint_store.get_store().capture(
                path,
                turn_id=ctx.turn_id,
                session_id=ctx.session_id,
                surface=ctx.surface,
                tool_name=ctx.tool_name,
                call_id=ctx.call_id,
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("checkpoint capture failed for %s: %s", path, exc)
            return None

    @staticmethod
    def _record_checkpoint_after(checkpoint_id: Optional[str], path: Path) -> None:
        if not checkpoint_id:
            return
        try:
            checkpoint_store.get_store().record_after(checkpoint_id, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint post-write record failed for %s: %s", path, exc)

    @staticmethod
    async def _finish_write(
        *,
        data: dict,
        ctx: "ToolCallContext",
        path: Path,
        before_text: Optional[str],
        after_text: str,
        checkpoint_id: Optional[str],
        warning: Optional[dict],
    ) -> dict:
        """Assemble the result and attach diagnostics.

        Runs *after* the per-path lock has been released, deliberately.
        The write, the checkpoint and the observation refresh are all done
        by this point, and diagnostics no longer reads the file at all:
        every checker takes its content on stdin, so both the pre-write
        and post-write text are passed in directly. That removes the
        re-read race and means a linter subprocess with a five second
        timeout cannot keep another subagent waiting on the same path.
        """
        if checkpoint_id:
            data["checkpoint_id"] = checkpoint_id
            data["turn_id"] = ctx.turn_id
        if warning:
            data["read_before_edit"] = warning
            data["warning"] = warning.get("message", "")

        diag = await diagnostics_mod.diagnose(
            path, before_text=before_text, after_text=after_text,
        )
        # Absent, not empty: an empty findings list reads to the model as
        # "checked, and clean", which is a stronger claim than we can make
        # when there was no checker to run.
        if diag is not None:
            data["diagnostics"] = diag
        return {"success": True, "status_code": 200, "data": data, "error": None}

    # ── write_file ────────────────────────────────────────────────

    async def _write_file(self, args: dict) -> dict:
        raw_path = args.get("path", "")
        if not str(raw_path):
            return {"success": False, "status_code": 400, "data": None, "error": "No path provided"}
        path = Path(raw_path).expanduser()
        content = args.get("content", "")

        ctx = require_context("coding_tools__write_file")
        tracker = file_state.get_tracker()

        def _guarded_write() -> "tuple[dict | None, dict | None]":
            """Blocking grant check, staleness check, checkpoint and write.

            One thread hop, the same reason Wave 1 used one: the stat, the
            hash, the pre-write snapshot and the write must see a
            consistent view of the file. Splitting it would let another
            coroutine interleave between the check and the write, which is
            precisely the race the lock exists to prevent.

            ``_check_write`` leads, both to keep a 403 ahead of a 409 and
            because it reads the workspace-grants file off the loop. See
            ``_read_file`` for why that was previously invisible.
            """
            denied = self._check_write(str(path))
            if denied:
                return denied, None
            refusal, warning = self._guard_write(ctx, path)
            if refusal:
                return refusal, None

            before_text = None
            if path.is_file():
                try:
                    before_text = self._read_verbatim(path)
                except OSError:
                    before_text = None

            checkpoint_id = self._capture_checkpoint(ctx, path)

            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_verbatim(path, content)

            self._record_checkpoint_after(checkpoint_id, path)
            tracker.note_write(ctx.session_id, path)
            return None, {
                "data": {"path": str(path), "bytes_written": len(content.encode())},
                "before_text": before_text,
                "after_text": content,
                "checkpoint_id": checkpoint_id,
                "warning": warning,
            }

        # The lock is taken on the event loop (asyncio.Lock is loop-affine
        # and must never be acquired from a worker thread) and held across
        # the whole hop. `spawn_subagents` runs up to six workers with full
        # coding_tools access, so without it two of them can both pass the
        # staleness check against the same fingerprint and the second
        # silently discards the first's write.
        async with tracker.lock_for(path):
            refusal, outcome = await asyncio.to_thread(_guarded_write)
        if refusal is not None:
            return refusal

        return await self._finish_write(ctx=ctx, path=path, **outcome)

    # ── edit_file ─────────────────────────────────────────────────

    async def _edit_file(self, args: dict) -> dict:
        path = Path(args.get("path", "")).expanduser()
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        replace_all = bool(args.get("replace_all", False))
        expected = args.get("expected_replacements")
        try:
            expected = int(expected) if expected not in (None, "") else None
        except (TypeError, ValueError):
            return {
                "success": False, "status_code": 400, "data": None,
                "error": "expected_replacements must be an integer",
            }

        ctx = require_context("coding_tools__edit_file")
        tracker = file_state.get_tracker()
        max_content_lines, max_needle_lines = self._edit_limits()

        def _guarded_edit() -> "tuple[dict | None, dict | None]":
            """Blocking grant check, staleness check, match and write.

            One thread hop, extending Wave 1's ``_read_match_write`` rather
            than replacing its reasoning: the existence check, the read,
            the match and the write must all see the same file contents.
            The fuzzy matcher lives in here too because it is the most
            expensive part of the whole call. The sliding-window
            strategies are O(file_lines x needle_lines), so on a
            4000-line file with a large needle it is millions of string
            comparisons, which is far too much to run on the event loop.

            ``_check_write`` leads for the same two reasons as in
            ``_write_file``: 403 before 409, and it reads the
            workspace-grants file.
            """
            denied = self._check_write(str(path))
            if denied:
                return denied, None

            refusal, warning = self._guard_write(ctx, path)
            if refusal:
                return refusal, None

            if not path.exists():
                return {
                    "success": False, "status_code": 404, "data": None,
                    "error": f"File not found: {path}",
                }, None
            if not old_text:
                # 400 rather than letting the matcher's empty_needle land
                # as a 409: a missing required argument is a bad request,
                # and this is the pre-existing contract.
                return {
                    "success": False, "status_code": 400, "data": None,
                    "error": "old_text is required",
                }, None

            content = self._read_verbatim(path)
            match = edit_matchers.find_edit_match(
                content, old_text,
                replace_all=replace_all,
                expected_replacements=expected,
                max_content_lines=max_content_lines,
                max_needle_lines=max_needle_lines,
            )
            if not match.ok:
                return self._edit_failure(path, match, warning), None

            checkpoint_id = self._capture_checkpoint(ctx, path)

            # Splice by offset. From `line_trimmed` onward the matched span
            # is not byte-identical to old_text, so content.replace() would
            # either find nothing or replace a different occurrence.
            new_content = edit_matchers.splice(content, match.candidates, new_text)
            self._write_verbatim(path, new_content)

            self._record_checkpoint_after(checkpoint_id, path)
            tracker.note_write(ctx.session_id, path)

            data = {
                "path": str(path),
                "replacements": len(match.candidates),
                # Reported so the model can tighten its next call, and so
                # the strategy mix is measurable.
                "match_strategy": match.strategy,
                "matched_lines": [
                    [c.start_line, c.end_line] for c in match.candidates
                ],
            }
            if match.requires_review:
                data["requires_review"] = True
                data["review_note"] = (
                    "Matched on the first and last line only (block_anchor); "
                    "the replaced interior was not verified against old_text. "
                    "Re-read the file to confirm the result."
                )
            return None, {
                "data": data,
                "before_text": content,
                "after_text": new_content,
                "checkpoint_id": checkpoint_id,
                "warning": warning,
            }

        async with tracker.lock_for(path):
            error, outcome = await asyncio.to_thread(_guarded_edit)
        if error is not None:
            return error

        return await self._finish_write(ctx=ctx, path=path, **outcome)
    @staticmethod
    def _edit_failure(path: Path, match, warning: Optional[dict] = None) -> dict:
        status = 404 if match.error_code == "not_found" else 409
        data: Dict[str, Any] = {
            "path": str(path),
            "error_code": match.error_code,
        }
        if warning:
            # Carried onto the failure too. A stale file that also fails to
            # match is the case where "this file changed under you" is the
            # single most useful thing we can say, and dropping it would
            # leave the model retrying the match instead of re-reading.
            data["read_before_edit"] = warning
        if match.strategy:
            data["match_strategy"] = match.strategy
        if match.candidates:
            data["matched_lines"] = [
                [c.start_line, c.end_line] for c in match.candidates
            ]
        if match.fuzzy_skipped:
            data["note"] = (
                "Only exact matching ran: the file or old_text exceeded the "
                "fallback-matcher size limit."
            )
        if match.closest is not None:
            # Hand back real file text rather than only "not found", so the
            # model can correct against what is actually there instead of
            # guessing again from the same stale memory.
            data["closest_match"] = {
                "start_line": match.closest.start_line,
                "end_line": match.closest.end_line,
                "similarity": match.closest.similarity,
                "text": match.closest.text,
            }
        return {
            "success": False,
            "status_code": status,
            "data": data,
            "error": match.message,
        }

    # ── revert_turn ───────────────────────────────────────────────

    async def _revert_turn(self, args: dict) -> dict:
        """Undo the file writes made while answering one user message.

        Exposed as a normal endpoint with ``safety_tier: "confirm"`` so
        FERAL's existing autonomy mode governs it: strict and hybrid ask
        the operator, loose runs it. That is the operator's call to make,
        not this tool's.
        """
        ctx = require_context("coding_tools__revert_turn")
        try:
            store = checkpoint_store.get_store()
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False, "status_code": 500, "data": None,
                "error": f"Checkpoint store unavailable: {exc}",
            }

        # SQLite queries, blob reads and the restores themselves are all
        # blocking, and a revert can touch every file a turn wrote. One
        # thread hop for the lot, for the same reason the writers use one.
        def _resolve_and_revert() -> "tuple[str, dict | None]":
            turn_id = str(args.get("turn_id") or "").strip()
            if not turn_id:
                turn_id = store.latest_turn(ctx.session_id or None) or ""
            if not turn_id:
                return "", None
            return turn_id, store.revert_turn(
                turn_id,
                force=bool(args.get("force", False)),
                dry_run=bool(args.get("dry_run", False)),
            )

        turn_id, result = await asyncio.to_thread(_resolve_and_revert)
        if result is None:
            return {
                "success": False, "status_code": 404,
                "data": {"bash_not_covered": True, "note": checkpoint_store.BASH_NOT_COVERED_NOTE},
                "error": "No checkpointed turn found to revert.",
            }
        success = bool(result.pop("success", False))
        error = result.pop("error", None)
        return {
            "success": success,
            "status_code": 200 if success else 409,
            "data": result,
            "error": error,
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
        # The pattern is model-authored, and Python's `re` backtracks, so
        # a catastrophic pattern hangs the brain with no timeout and no
        # way to interrupt it. Verified: `(a+)+$` against 30 characters
        # does not return. The ripgrep path above needs no guard because
        # Rust's regex engine is linear-time by construction; only this
        # fallback is exposed.
        try:
            regex = compile_safe_regex(pattern)
        except UnsafePatternError as exc:
            return {
                "success": False, "status_code": 400, "data": None,
                "error": (
                    f"Unsafe regex rejected: {exc}. This pattern can take "
                    f"exponential time on Python's backtracking engine. "
                    f"Rewrite it without nested or stacked quantifiers, or "
                    f"install ripgrep, whose engine has no such failure mode."
                ),
            }
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
