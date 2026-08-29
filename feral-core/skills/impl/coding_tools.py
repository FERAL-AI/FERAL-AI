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
import atexit
import base64
import dataclasses
import logging
import os
import re
import signal
import time
import uuid
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
# Foreground ceiling. ENFORCED, NOT CLAMPED: the old code did
# ``min(requested, 120)``, so a caller that asked for 600s was told
# "timed out after 120s" and had no way to learn that the number it
# passed had been quietly replaced. A request above this ceiling is now
# a 400 that names the ceiling and points at run_in_background.
BASH_MAX_TIMEOUT = 600
# Background jobs are bounded too, an unbounded job is an orphan
# waiting to happen. Default 1h, hard ceiling 24h.
BACKGROUND_DEFAULT_TIMEOUT = 3_600
BACKGROUND_MAX_TIMEOUT = 86_400
# Per-stream ring buffer for a background job. 2 000 lines x 4 000 chars
# is ~8 MB worst case per stream; the ring keeps the NEWEST lines and
# reports how many it dropped, so nothing is lost silently.
BG_MAX_BUFFER_LINES = 2_000
BG_MAX_LINE_CHARS = 4_000
BG_MAX_RUNNING_PER_SESSION = 8
BG_MAX_RUNNING_TOTAL = 32
# Finished jobs stay readable for a while so a poller can collect the
# tail, then get pruned. Both bounds apply.
BG_FINISHED_RETENTION = 32
BG_FINISHED_TTL_SEC = 900
BG_OUTPUT_DEFAULT_MAX_LINES = 500
# Text read ceiling (unchanged). Images get their own, larger ceiling -
# see ``_image_byte_ceiling``.
MAX_TEXT_READ_BYTES = 2_000_000
# Reference-codebase ergonomics (AUDIT-r14 round3 engine spec #4):
# default grep to file names, paginate, relativize paths to keep tool
# output compact for the LLM context window.
GREP_DEFAULT_HEAD_LIMIT = 250
GLOB_DEFAULT_HEAD_LIMIT = 100
# The command deny list lives in one place:
# ``security.sandbox_policy.SandboxPolicy._COMMAND_DENY_FLOOR``, applied
# by ``resolve_execution_mode`` further down this method on every path
# that can execute.
#
# A second, hand-rolled ``DANGEROUS_COMMANDS`` regex used to sit here
# and it blocked nothing that mattered. Its trailing ``\b`` required a
# word character, and ``rm -rf /`` ends with ``/``, so the boundary
# could never match: ``rm -rf /home`` was caught and ``rm -rf /`` was
# not, which is exactly backwards. Its fork-bomb branch contained an
# unescaped ``()`` that compiled to an empty capture group and matched
# nothing at all.
#
# It was never the real boundary, so removing it opens nothing. The harm
# was the false confidence, and that two deny lists drift when only one
# of them is reviewed.


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


# ── background jobs ───────────────────────────────────────────────
#
# Background execution runs on ``process/supervisor`` rather than a
# second private subprocess implementation: the supervisor already owns
# overall/no-output timeouts, TERM->KILL escalation, a run registry, and
# scope-cancel. What it lacked was bounded buffering, incremental reads
# and process-group kills; those were added there (see
# ``process/supervisor/buffer.py`` and ``adapters/child.py``) so every
# future caller gets them too.

def _stay_awake_acquire(reason: str = "") -> None:
    """Best effort. A machine that will not hold an assertion still runs."""
    try:
        from system.preflight import StayAwake
        StayAwake.acquire(reason)
    except Exception:
        logger.debug("could not inhibit sleep for %s", reason, exc_info=True)


def _stay_awake_release() -> None:
    try:
        from system.preflight import StayAwake
        StayAwake.release()
    except Exception:
        logger.debug("could not release the sleep assertion", exc_info=True)


_LIVE_BACKGROUND_PGIDS: "dict[str, int]" = {}


def _kill_orphan_background_jobs() -> None:
    """Last-resort reaper: SIGKILL every background job's process group.

    Registered with :mod:`atexit`. The event loop may already be gone by
    then, so this deliberately uses raw ``os.killpg`` rather than the
    supervisor's async kill ladder.
    """
    for job_id, pgid in list(_LIVE_BACKGROUND_PGIDS.items()):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        _LIVE_BACKGROUND_PGIDS.pop(job_id, None)


atexit.register(_kill_orphan_background_jobs)


@dataclasses.dataclass
class _BackgroundJob:
    """One backgrounded shell command and the cursors into its output."""

    job_id: str
    session_id: str
    command: str
    cwd: str
    timeout_sec: int
    handle: Any
    loop_id: int
    started_at: float
    stdout_cursor: int = 0
    stderr_cursor: int = 0

    @property
    def pid(self) -> int:
        return int(getattr(self.handle, "pid", -1))

    @property
    def finished(self) -> bool:
        return bool(getattr(self.handle, "finished", False))

    def status(self) -> str:
        record = getattr(self.handle, "record", None)
        if record is None:
            return "running"
        reason = getattr(record, "kill_reason", None)
        if reason in (None, "exit"):
            return "completed"
        if reason == "overall_timeout":
            return "timed_out"
        return "killed"

    def exit_code(self) -> "int | None":
        record = getattr(self.handle, "record", None)
        return None if record is None else getattr(record, "exit_code", None)

    def kill_reason(self) -> "str | None":
        record = getattr(self.handle, "record", None)
        return None if record is None else getattr(record, "kill_reason", None)


# ── binary / image detection ──────────────────────────────────────
#
# Media types are limited to the set ``agents/multimodal_blocks.py``
# knows how to sniff and deliver (``_B64_MAGIC`` there). Reading a file
# type the image pipeline cannot carry as if it could would just move the
# mojibake one layer up.
_IMAGE_MAGIC: "tuple[tuple[bytes, str], ...]" = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

# Non-image binaries we can name in the refusal, so the model gets a fact
# instead of "not text".
_BINARY_MAGIC: "tuple[tuple[bytes, str], ...]" = (
    (b"%PDF-", "PDF document"),
    (b"\x7fELF", "ELF binary"),
    (b"\xcf\xfa\xed\xfe", "Mach-O binary"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal binary or Java class"),
    (b"PK\x03\x04", "ZIP archive (or .docx/.xlsx/.jar/.whl)"),
    (b"\x1f\x8b", "gzip archive"),
    (b"BZh", "bzip2 archive"),
    (b"\xfd7zXZ\x00", "xz archive"),
    (b"SQLite format 3\x00", "SQLite database"),
    (b"ID3", "MP3 audio"),
    (b"OggS", "Ogg media"),
    (b"\x00\x00\x00\x18ftyp", "MP4 video"),
    (b"\x00\x00\x00\x20ftyp", "MP4 video"),
    (b"\x00asm", "WebAssembly module"),
)


def _sniff_image_media_type(head: bytes) -> "str | None":
    """Return the image media type from magic bytes, or None."""
    for magic, media_type in _IMAGE_MAGIC:
        if head.startswith(magic):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _sniff_binary_kind(head: bytes) -> str:
    """Name a non-image binary format, or fall back to a generic label."""
    for magic, label in _BINARY_MAGIC:
        if head.startswith(magic):
            return label
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "UTF-16 text (not decodable as UTF-8)"
    return "binary data"


def _image_byte_ceiling() -> int:
    """Largest image ``read_file`` will return, in bytes on disk.

    Derived from the image pipeline's own ceiling
    (``FERAL_TOOL_IMAGE_MAX_B64_CHARS``, default 5 000 000 base64 chars)
    so this tool never hands the pipeline a payload the pipeline would
    then refuse whole. base64 costs 4 chars per 3 bytes, plus the data:
    URL prefix, so we take 3/4 of the char budget minus a small margin.
    """
    chars = 5_000_000
    try:  # source of truth, read (never edited) from the image pipeline
        from agents.multimodal_blocks import _max_image_b64_chars

        chars = int(_max_image_b64_chars())
    except Exception:  # pragma: no cover - stripped build / import cycle
        raw = (os.environ.get("FERAL_TOOL_IMAGE_MAX_B64_CHARS") or "").strip()
        if raw.isdigit() and int(raw) > 0:
            chars = int(raw)
    return max(1, (chars - 128) * 3 // 4)


def _png_dimensions(data: bytes) -> "tuple[int, int] | None":
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


def _gif_dimensions(data: bytes) -> "tuple[int, int] | None":
    if len(data) < 10:
        return None
    return (
        int.from_bytes(data[6:8], "little"),
        int.from_bytes(data[8:10], "little"),
    )


def _image_dimensions(media_type: str, data: bytes) -> "tuple[int, int] | None":
    """Dimensions for the formats whose header is trivially parseable.

    JPEG/WebP/BMP are deliberately NOT guessed at: returning nothing is
    honest, returning a wrong number is not.
    """
    if media_type == "image/png":
        return _png_dimensions(data)
    if media_type == "image/gif":
        return _gif_dimensions(data)
    return None


# ── argument validation ───────────────────────────────────────────


def _unexpected_args(
    args: dict,
    allowed: "set[str]",
    endpoint: str,
) -> "dict | None":
    """Refuse arguments the endpoint does not implement.

    Silently ignoring an option the caller passed is the defect this
    whole pass exists to remove: the caller believes ``-i`` took effect
    and reads the (wrong) result as ground truth. Keys starting with
    ``_`` are internal plumbing (e.g. ``_feral_require_sandbox``) and are
    always allowed.
    """
    unknown = sorted(
        k for k in args
        if not str(k).startswith("_") and k not in allowed
    )
    if not unknown:
        return None
    return {
        "success": False,
        "status_code": 400,
        "data": {"unsupported_params": unknown, "supported_params": sorted(allowed)},
        "error": (
            f"coding_tools__{endpoint} does not support "
            f"{', '.join(repr(u) for u in unknown)}. Supported: "
            f"{', '.join(sorted(allowed))}. Nothing was executed, these "
            f"arguments are refused rather than ignored, so a result you "
            f"read is never one where your options were dropped."
        ),
    }


# ── grep options ──────────────────────────────────────────────────
#
# Names follow the reference coding-agent surface (``-i``, ``-A``,
# ``-B``, ``-C``, ``type``, ``multiline``); the spelled-out aliases are
# accepted too because a model that writes ``case_insensitive`` means the
# same thing and should not get a refusal for spelling.
GREP_ALIASES: "dict[str, tuple[str, ...]]" = {
    "ignore_case": ("-i", "case_insensitive", "ignore_case"),
    "after": ("-A", "after_context"),
    "before": ("-B", "before_context"),
    "context": ("-C", "context", "context_lines"),
    "multiline": ("multiline",),
    "file_type": ("type", "file_type"),
}
GREP_ALLOWED_ARGS = {
    "pattern", "path", "include", "output_mode", "head_limit", "offset",
} | {alias for aliases in GREP_ALIASES.values() for alias in aliases}

# Field separators for rows that carry context. rg's defaults (':' for a
# match row, '-' for a context row) are both legal path characters, so
# `a-b:1:x` cannot be parsed back reliably; these two can appear in
# neither a POSIX path nor a realistic source line.
#
# NOT \x1e (record separator), which would be the obvious pick: Python's
# ``str.splitlines()`` treats \x1c, \x1d, \x1e and \x85 as line
# boundaries, so every context row got shredded into three fragments and
# the parser then dropped all of them, context silently vanished on the
# ripgrep path while the fallback returned it. Verified by running it.
# \x1f (unit separator) and \x01 (SOH) are not in that set.
GREP_MATCH_SEP = "\x1f"
GREP_CONTEXT_SEP = "\x01"
GREP_MAX_CONTEXT = 100

# What the pure-Python fallback knows about ``type``. ripgrep knows ~800
# types; this is the documented subset, and anything else is refused
# rather than quietly filtered by a different rule.
FALLBACK_TYPE_GLOBS: "dict[str, tuple[str, ...]]" = {
    "py": (".py", ".pyi"),
    "js": (".js", ".jsx", ".mjs", ".cjs"),
    "ts": (".ts", ".tsx", ".mts", ".cts"),
    "swift": (".swift",),
    "rust": (".rs",),
    "go": (".go",),
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh"),
    "objc": (".m", ".mm"),
    "ruby": (".rb",),
    "php": (".php",),
    "sh": (".sh", ".bash", ".zsh"),
    "md": (".md", ".markdown"),
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "toml": (".toml",),
    "html": (".html", ".htm"),
    "css": (".css", ".scss", ".sass"),
    "sql": (".sql",),
    "xml": (".xml",),
    "config": (".cfg", ".ini", ".conf"),
}

_DEFAULT_GREP_OPTS: "dict[str, Any]" = {
    "ignore_case": False,
    "after": 0,
    "before": 0,
    "multiline": False,
    "file_type": "",
}


def _grep_options(args: dict, output_mode: str) -> "tuple[dict, dict | None]":
    """Normalise the grep flags, or return a refusal.

    Every option is either applied or refused, never accepted and
    dropped. Conflicting aliases (``-i: true`` next to
    ``case_insensitive: false``) are a refusal too, because guessing
    which one the caller meant is the same failure in a nicer costume.
    """

    def pick(canonical: str) -> "tuple[Any, str | None, dict | None]":
        seen: "list[tuple[str, Any]]" = [
            (name, args[name])
            for name in GREP_ALIASES[canonical]
            if name in args and args[name] is not None
        ]
        if not seen:
            return None, None, None
        values = {repr(v) for _n, v in seen}
        if len(values) > 1:
            return None, None, {
                "success": False, "status_code": 400, "data": None,
                "error": (
                    "Conflicting values for the same option: "
                    + ", ".join(f"{n}={v!r}" for n, v in seen)
                    + ". Pass it once."
                ),
            }
        return seen[0][1], seen[0][0], None

    opts = dict(_DEFAULT_GREP_OPTS)

    raw, name, err = pick("ignore_case")
    if err:
        return opts, err
    if raw is not None:
        coerced = _as_bool(raw)
        if coerced is None:
            return opts, {
                "success": False, "status_code": 400, "data": None,
                "error": f"{name} must be a boolean, got {raw!r}.",
            }
        opts["ignore_case"] = coerced

    raw, name, err = pick("multiline")
    if err:
        return opts, err
    if raw is not None:
        coerced = _as_bool(raw)
        if coerced is None:
            return opts, {
                "success": False, "status_code": 400, "data": None,
                "error": f"{name} must be a boolean, got {raw!r}.",
            }
        opts["multiline"] = coerced

    raw, name, err = pick("file_type")
    if err:
        return opts, err
    if raw is not None:
        if not isinstance(raw, str) or not raw.strip():
            return opts, {
                "success": False, "status_code": 400, "data": None,
                "error": f"{name} must be a non-empty string such as 'py' or 'ts'.",
            }
        opts["file_type"] = raw.strip()

    # -C sets both sides; an explicit -A/-B then overrides its own side.
    for canonical in ("context", "after", "before"):
        raw, name, err = pick(canonical)
        if err:
            return opts, err
        if raw is None:
            continue
        try:
            count = int(raw)
        except (TypeError, ValueError):
            return opts, {
                "success": False, "status_code": 400, "data": None,
                "error": f"{name} must be an integer number of lines, got {raw!r}.",
            }
        if count < 0 or count > GREP_MAX_CONTEXT:
            return opts, {
                "success": False, "status_code": 400, "data": None,
                "error": (
                    f"{name}={count} is out of range; context lines must be "
                    f"0-{GREP_MAX_CONTEXT}."
                ),
            }
        if canonical == "context":
            opts["after"] = count
            opts["before"] = count
        else:
            opts[canonical] = count

    if (opts["after"] or opts["before"]) and output_mode != "content":
        return opts, {
            "success": False, "status_code": 400, "data": None,
            "error": (
                "Context lines (-A/-B/-C) only apply to "
                "output_mode='content'; they are meaningless for "
                f"{output_mode!r}, which returns no lines. Set "
                "output_mode='content' or drop the context option."
            ),
        }
    return opts, None


def _parse_context_row(line: str) -> "dict | None":
    """Parse one rg row emitted with the control-character separators.

    Returns ``{file, line, text, is_context}``, or None for rg's ``--``
    group-break rows (which carry no fields).
    """
    match_at = line.find(GREP_MATCH_SEP)
    ctx_at = line.find(GREP_CONTEXT_SEP)
    if match_at < 0 and ctx_at < 0:
        return None
    if match_at >= 0 and (ctx_at < 0 or match_at < ctx_at):
        sep, is_context = GREP_MATCH_SEP, False
    else:
        sep, is_context = GREP_CONTEXT_SEP, True
    parts = line.split(sep, 2)
    if len(parts) < 3:
        return {"text": line, "is_context": is_context}
    return {
        "file": _relativize(parts[0]),
        "line": parts[1],
        "text": parts[2],
        "is_context": is_context,
    }


def _fallback_rows_for_file(
    fp: Path, text: str, regex: "re.Pattern[str]", opts: dict,
) -> "list[dict]":
    """Match one file in the pure-Python path, with context + multiline.

    Context rows are marked ``is_context: True`` exactly as in the
    ripgrep path, and a line that is both a match and another match's
    context is emitted once, as a match.
    """
    lines = text.splitlines()
    hit_lines: "set[int]" = set()
    if opts["multiline"]:
        for m in regex.finditer(text):
            # Every line the match SPANS counts as a match line, which is
            # what ripgrep prints for a multiline hit. Emitting only the
            # start line here would make the two engines disagree on the
            # same file.
            start = text.count("\n", 0, m.start()) + 1
            end = text.count("\n", 0, max(m.start(), m.end() - 1)) + 1
            hit_lines.update(range(start, end + 1))
    else:
        for i, line in enumerate(lines, 1):
            if regex.search(line):
                hit_lines.add(i)
    if not hit_lines:
        return []

    before, after = int(opts["before"]), int(opts["after"])
    wanted: "set[int]" = set()
    for n in hit_lines:
        wanted.update(range(max(1, n - before), min(len(lines), n + after) + 1))

    rel = _relativize(fp)
    rows = []
    for n in sorted(wanted):
        row = {"file": rel, "line": str(n), "text": lines[n - 1].strip()}
        if before or after:
            row["is_context"] = n not in hit_lines
        rows.append(row)
    return rows


def _annotate_options(data: dict, opts: dict, *, engine: str) -> None:
    """Echo the options that were actually applied, plus the engine.

    A caller can then SEE that ``-i`` took effect rather than trusting
    that it did.
    """
    data["engine"] = engine
    data["options_applied"] = {
        "case_insensitive": bool(opts["ignore_case"]),
        "multiline": bool(opts["multiline"]),
        "after_context": int(opts["after"]),
        "before_context": int(opts["before"]),
        "type": opts["file_type"] or None,
    }


def _as_bool(value: Any) -> "bool | None":
    """Coerce a model-authored boolean. Returns None if not boolean-ish."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1", "on"):
            return True
        if lowered in ("false", "no", "0", "off", ""):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


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
        # Background-job state. The skill is a process-wide singleton
        # (``skills.impl.register_skill`` instantiates once), so this map
        # is the process's job table. Jobs are keyed by id and carry
        # their session id: one session can never read or kill another
        # session's job.
        self._bg_jobs: "dict[str, _BackgroundJob]" = {}
        # One supervisor per event loop. ``asyncio.Lock`` binds to the
        # loop that first awaits it, and a singleton skill outlives any
        # single loop (tests build a fresh loop per case), so a supervisor
        # cached across loops would raise "bound to a different event
        # loop" on the second use.
        self._supervisors: "dict[int, Any]" = {}
        # Strong references to the per-job watchers that drop a finished
        # job's pgid from the atexit reaper. The loop holds tasks only
        # weakly (AUDIT-FIXES F-06), and the set self-prunes on done.
        self._bg_watchers: "set[asyncio.Task]" = set()

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
            "bash_output": self._bash_output,
            "kill_bash": self._kill_bash,
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

        ``run_in_background: true`` hands the command to
        ``process/supervisor`` and returns a job id immediately. Every
        check below (destructive-command scan, quote balance, file-state
        invalidation, and the full ``resolve_execution_mode`` grant /
        sandbox decision) runs FIRST and identically for both lanes -
        a background lane that skipped them would be a privilege
        escalation, so the branch sits after the decision, not before it.
        """
        bad_args = _unexpected_args(
            args,
            {"command", "cwd", "timeout", "run_in_background", "description"},
            "bash",
        )
        if bad_args:
            return bad_args

        command = args.get("command", "")
        if not command:
            return {"success": False, "status_code": 400, "data": None, "error": "No command provided"}

        background = _as_bool(args.get("run_in_background", False))
        if background is None:
            return {
                "success": False, "status_code": 400, "data": None,
                "error": "run_in_background must be a boolean.",
            }

        quote_err = _check_shell_quotes(command)
        if quote_err:
            return {"success": False, "status_code": 400, "data": None, "error": quote_err}

        ceiling = BACKGROUND_MAX_TIMEOUT if background else BASH_MAX_TIMEOUT
        default_timeout = BACKGROUND_DEFAULT_TIMEOUT if background else BASH_TIMEOUT
        raw_timeout = args.get("timeout", default_timeout)
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            return {
                "success": False, "status_code": 400, "data": None,
                "error": f"timeout must be an integer number of seconds, got {raw_timeout!r}.",
            }
        if timeout <= 0:
            return {
                "success": False, "status_code": 400, "data": None,
                "error": "timeout must be a positive number of seconds.",
            }
        if timeout > ceiling:
            # Refused, not clamped. The old code silently replaced the
            # requested value with 120 and then reported the timeout as
            # if the caller had asked for it.
            hint = (
                " Pass run_in_background: true for work that legitimately "
                f"runs longer (background ceiling {BACKGROUND_MAX_TIMEOUT}s)."
                if not background else ""
            )
            return {
                "success": False, "status_code": 400,
                "data": {"requested_timeout": timeout, "max_timeout": ceiling},
                "error": (
                    f"timeout={timeout}s exceeds the maximum of {ceiling}s for "
                    f"{'background' if background else 'foreground'} bash. "
                    f"Nothing was executed.{hint}"
                ),
            }

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

        if decision.mode == MODE_REFUSED:
            return self._refuse_bash(decision)

        if background:
            if decision.mode == MODE_DOCKER:
                # Refused rather than silently downgraded to a foreground
                # Docker run: the caller asked for a job id and would
                # otherwise block for the full command with no way to see
                # that its request had been dropped.
                return {
                    "success": False, "status_code": 501,
                    "data": {
                        "execution_mode": MODE_DOCKER,
                        "run_in_background": True,
                    },
                    "error": (
                        "run_in_background is not supported in the Docker "
                        "sandbox lane; the supervisor manages host processes "
                        "only. Re-run without run_in_background, or run it in "
                        "a granted host workspace."
                    ),
                }
            return await self._start_background_job(command, decision, timeout)

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
            return {
                "success": False, "status_code": 408,
                "data": {"timeout_sec": timeout, "max_timeout": BASH_MAX_TIMEOUT},
                "error": (
                    f"Command timed out after {timeout}s (the timeout you "
                    f"asked for; foreground maximum is {BASH_MAX_TIMEOUT}s). "
                    f"For longer work use run_in_background: true and poll "
                    f"coding_tools__bash_output."
                ),
            }

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
                # Echoed, not interpreted. Accepted because agent
                # harnesses send it habitually and refusing it would be
                # friction for nothing; echoing it is the only claim
                # about it this tool can actually keep.
                "description": args.get("description"),
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

    # ── background bash: start / poll / kill ──────────────────────

    def _supervisor(self):
        """The :class:`ProcessSupervisor` for the running event loop."""
        from process.supervisor import create_process_supervisor

        loop_id = id(asyncio.get_running_loop())
        supervisor = self._supervisors.get(loop_id)
        if supervisor is None:
            supervisor = create_process_supervisor()
            self._supervisors[loop_id] = supervisor
        return supervisor

    def _prune_background_jobs(self) -> None:
        """Drop finished jobs that are old or in excess of the retention.

        Running jobs are never pruned, they are killed explicitly, or by
        their own wall-clock timeout, or by ``clear_session``.
        """
        now = time.monotonic()
        finished = [
            (job_id, job) for job_id, job in self._bg_jobs.items() if job.finished
        ]
        for job_id, job in finished:
            _LIVE_BACKGROUND_PGIDS.pop(job_id, None)
            if now - job.started_at > BG_FINISHED_TTL_SEC:
                self._bg_jobs.pop(job_id, None)
        finished = [
            (job_id, job) for job_id, job in self._bg_jobs.items() if job.finished
        ]
        if len(finished) > BG_FINISHED_RETENTION:
            finished.sort(key=lambda pair: pair[1].started_at)
            for job_id, _job in finished[: len(finished) - BG_FINISHED_RETENTION]:
                self._bg_jobs.pop(job_id, None)

    async def _start_background_job(self, command: str, decision, timeout: int) -> dict:
        """Spawn ``command`` under the process supervisor, return a job id.

        Called only after every safety check in ``_bash`` has passed.
        """
        ctx = require_context("coding_tools__bash")
        self._prune_background_jobs()

        running = [j for j in self._bg_jobs.values() if not j.finished]
        mine = [j for j in running if j.session_id == ctx.session_id]
        if len(mine) >= BG_MAX_RUNNING_PER_SESSION:
            return {
                "success": False, "status_code": 429,
                "data": {
                    "running_jobs": [j.job_id for j in mine],
                    "limit": BG_MAX_RUNNING_PER_SESSION,
                },
                "error": (
                    f"This session already has {len(mine)} background jobs "
                    f"running (limit {BG_MAX_RUNNING_PER_SESSION}). Kill one "
                    f"with coding_tools__kill_bash first."
                ),
            }
        if len(running) >= BG_MAX_RUNNING_TOTAL:
            return {
                "success": False, "status_code": 429,
                "data": {"limit": BG_MAX_RUNNING_TOTAL},
                "error": (
                    f"{len(running)} background jobs are running across all "
                    f"sessions (limit {BG_MAX_RUNNING_TOTAL})."
                ),
            }

        supervisor = self._supervisor()
        try:
            handle = await supervisor.run(
                ["/bin/sh", "-c", command],
                scope_key=ctx.session_id,
                overall_timeout_sec=float(timeout),
                cwd=decision.cwd,
                max_buffered_lines=BG_MAX_BUFFER_LINES,
                max_line_chars=BG_MAX_LINE_CHARS,
                # Own process group, so killing the job kills anything the
                # shell spawned rather than leaving orphans behind.
                start_new_session=True,
            )
        except OSError as exc:
            return {
                "success": False, "status_code": 500, "data": None,
                "error": f"Could not start background job: {exc}",
            }

        job_id = f"bg_{uuid.uuid4().hex[:12]}"
        job = _BackgroundJob(
            job_id=job_id,
            session_id=ctx.session_id,
            command=command,
            cwd=decision.cwd,
            timeout_sec=timeout,
            handle=handle,
            loop_id=id(asyncio.get_running_loop()),
            started_at=time.monotonic(),
        )
        self._bg_jobs[job_id] = job
        # Hold the machine awake for the life of the job. A detached build
        # or test run dies when the Mac sleeps, and the failure surfaces as
        # a truncated log with no cause: the process is simply gone. The
        # assertion is reference counted, so N concurrent jobs share one
        # and the last to finish releases it.
        _stay_awake_acquire(f"background job {job_id}")
        try:
            _LIVE_BACKGROUND_PGIDS[job_id] = os.getpgid(handle.pid)
        except (OSError, ProcessLookupError):
            _LIVE_BACKGROUND_PGIDS[job_id] = handle.pid
        # Forget the pgid the moment the job ends. Waiting for the next
        # prune would leave a dead pgid registered with the atexit
        # reaper, and pids are reused: at exit that entry could name an
        # unrelated process group.
        watcher = asyncio.create_task(self._forget_pgid_when_done(job_id, handle))
        self._bg_watchers.add(watcher)
        watcher.add_done_callback(self._bg_watchers.discard)

        return {
            "success": True,
            "status_code": 202,
            "data": {
                "job_id": job_id,
                "status": "running",
                "pid": handle.pid,
                "command": command,
                "cwd": decision.cwd,
                "timeout_sec": timeout,
                "execution_mode": decision.mode,
                "workspace": decision.workspace,
                "sandbox": "host",
                "read_output_with": "coding_tools__bash_output",
                "kill_with": "coding_tools__kill_bash",
                "output_buffer": {
                    "max_lines_per_stream": BG_MAX_BUFFER_LINES,
                    "max_chars_per_line": BG_MAX_LINE_CHARS,
                    "policy": (
                        "newest lines are kept; anything dropped is reported "
                        "as dropped_stdout_lines / dropped_stderr_lines"
                    ),
                },
                "note": (
                    f"Started in the background. It is killed automatically "
                    f"after {timeout}s. Poll coding_tools__bash_output with "
                    f"this job_id; each poll returns only output produced "
                    f"since the previous poll."
                ),
            },
            "error": None,
        }

    @staticmethod
    async def _forget_pgid_when_done(job_id: str, handle) -> None:
        """Drop ``job_id`` from the atexit reaper once its process exits."""
        try:
            await handle.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("watcher for %s ended abnormally", job_id, exc_info=True)
        finally:
            _LIVE_BACKGROUND_PGIDS.pop(job_id, None)
            # Paired with the acquire in the start path. In `finally` so a
            # crashed or cancelled watcher still drops its reference; a
            # leaked assertion keeps the machine awake indefinitely.
            _stay_awake_release()

    def _lookup_job(self, args: dict, endpoint: str) -> "tuple[_BackgroundJob | None, dict | None]":
        """Resolve ``job_id`` for the calling session, or build the error."""
        job_id = str(args.get("job_id") or "").strip()
        ctx = require_context(f"coding_tools__{endpoint}")
        if not job_id:
            return None, {
                "success": False, "status_code": 400, "data": None,
                "error": "job_id is required (returned by coding_tools__bash with run_in_background).",
            }
        self._prune_background_jobs()
        job = self._bg_jobs.get(job_id)
        # A job belongs to the session that started it. Cross-session
        # reads are indistinguishable from "no such job" on purpose.
        if job is None or job.session_id != ctx.session_id:
            live = sorted(
                j.job_id for j in self._bg_jobs.values()
                if j.session_id == ctx.session_id
            )
            return None, {
                "success": False, "status_code": 404,
                "data": {"job_id": job_id, "jobs_in_this_session": live},
                "error": (
                    f"No background job {job_id!r} in this session. "
                    + (f"Known job ids: {', '.join(live)}." if live else
                       "This session has no background jobs.")
                ),
            }
        if job.loop_id != id(asyncio.get_running_loop()):
            return None, {
                "success": False, "status_code": 409,
                "data": {"job_id": job_id},
                "error": (
                    f"Background job {job_id} was started on a different "
                    f"event loop and can no longer be controlled from here."
                ),
            }
        return job, None

    async def _bash_output(self, args: dict) -> dict:
        """Return output a background job produced since the last poll."""
        bad_args = _unexpected_args(
            args, {"job_id", "filter", "max_lines"}, "bash_output"
        )
        if bad_args:
            return bad_args

        job, error = self._lookup_job(args, "bash_output")
        if error is not None:
            return error
        assert job is not None

        try:
            max_lines = int(args.get("max_lines", BG_OUTPUT_DEFAULT_MAX_LINES))
        except (TypeError, ValueError):
            return {
                "success": False, "status_code": 400, "data": None,
                "error": "max_lines must be an integer (0 = no per-call cap).",
            }
        if max_lines < 0:
            return {
                "success": False, "status_code": 400, "data": None,
                "error": "max_lines must be >= 0 (0 = no per-call cap).",
            }

        pattern = args.get("filter")
        regex = None
        if pattern:
            try:
                regex = compile_safe_regex(str(pattern))
            except UnsafePatternError as exc:
                return {
                    "success": False, "status_code": 400, "data": None,
                    "error": f"Unsafe filter regex rejected: {exc}",
                }
            except re.error as exc:
                return {
                    "success": False, "status_code": 400, "data": None,
                    "error": f"Invalid filter regex: {exc}",
                }

        out_buf = job.handle.stdout_buffer
        err_buf = job.handle.stderr_buffer
        out_lines, out_cursor, out_skipped = out_buf.read_since(job.stdout_cursor, max_lines)
        err_lines, err_cursor, err_skipped = err_buf.read_since(job.stderr_cursor, max_lines)
        # Cursors advance past every CONSUMED line, including ones the
        # filter removed, a filtered poll is a read, not a peek, and the
        # response says so.
        job.stdout_cursor = out_cursor
        job.stderr_cursor = err_cursor

        if regex is not None:
            out_lines = [ln for ln in out_lines if regex.search(ln)]
            err_lines = [ln for ln in err_lines if regex.search(ln)]

        status = job.status()
        data = {
            "job_id": job.job_id,
            "status": status,
            "exit_code": job.exit_code(),
            "kill_reason": job.kill_reason(),
            "pid": job.pid,
            "command": job.command,
            "cwd": job.cwd,
            "runtime_sec": round(time.monotonic() - job.started_at, 3),
            "stdout": "\n".join(out_lines)[:MAX_OUTPUT],
            "stderr": "\n".join(err_lines)[:MAX_OUTPUT],
            "stdout_lines": len(out_lines),
            "stderr_lines": len(err_lines),
            "dropped_stdout_lines": out_skipped,
            "dropped_stderr_lines": err_skipped,
            "has_more_output": (
                out_cursor < out_buf.total_appended
                or err_cursor < err_buf.total_appended
            ),
            "filter": str(pattern) if pattern else None,
        }
        if regex is not None:
            data["note"] = (
                "filter applied after reading: lines that did not match were "
                "consumed and will not appear in a later poll."
            )
        if status == "timed_out":
            data["timeout_sec"] = job.timeout_sec
        return {"success": True, "status_code": 200, "data": data, "error": None}

    async def _kill_bash(self, args: dict) -> dict:
        """Terminate a background job (SIGTERM, then SIGKILL)."""
        bad_args = _unexpected_args(args, {"job_id", "force"}, "kill_bash")
        if bad_args:
            return bad_args

        job, error = self._lookup_job(args, "kill_bash")
        if error is not None:
            return error
        assert job is not None

        force = _as_bool(args.get("force", False))
        if force is None:
            return {
                "success": False, "status_code": 400, "data": None,
                "error": "force must be a boolean.",
            }

        already = job.finished
        if not already:
            if force:
                job.handle._trigger_kill("manual_cancel")
                job.handle._adapter.kill(grace_sec=0.0)
            else:
                job.handle.cancel("manual_cancel")
            try:
                # Report the real outcome rather than "kill requested":
                # SIGTERM plus the supervisor's 5s SIGKILL ladder means a
                # live process is gone well inside this window.
                await asyncio.wait_for(job.handle.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning("background job %s did not exit after kill", job.job_id)
        _LIVE_BACKGROUND_PGIDS.pop(job.job_id, None)

        out_buf = job.handle.stdout_buffer
        err_buf = job.handle.stderr_buffer
        tail_out, _c1, _s1 = out_buf.read_since(job.stdout_cursor, 0)
        tail_err, _c2, _s2 = err_buf.read_since(job.stderr_cursor, 0)
        job.stdout_cursor = out_buf.total_appended
        job.stderr_cursor = err_buf.total_appended

        return {
            "success": True,
            "status_code": 200,
            "data": {
                "job_id": job.job_id,
                "status": job.status(),
                "was_already_finished": already,
                "exit_code": job.exit_code(),
                "kill_reason": job.kill_reason(),
                "signal": "SIGKILL" if force else "SIGTERM then SIGKILL after 5s",
                "runtime_sec": round(time.monotonic() - job.started_at, 3),
                "final_stdout": "\n".join(tail_out)[:MAX_OUTPUT],
                "final_stderr": "\n".join(tail_err)[:MAX_OUTPUT],
            },
            "error": None,
        }

    async def clear_session(self, session_id: str) -> int:
        """Kill every background job belonging to ``session_id``.

        Called on session teardown. NOTE: nothing calls this yet, the
        orchestrator's ``on_session_disconnect`` fan-out lives in files
        this lane does not own. Until it is wired, background jobs are
        still bounded by their wall-clock timeout, the per-session job
        cap, and the atexit reaper, so none of them can outlive the
        process.
        """
        victims = [
            job for job in self._bg_jobs.values()
            if job.session_id == session_id and not job.finished
        ]
        for job in victims:
            try:
                job.handle.cancel("manual_cancel")
            except Exception:  # pragma: no cover - best effort teardown
                logger.debug("kill of %s failed", job.job_id, exc_info=True)
        for job_id in [
            j.job_id for j in self._bg_jobs.values() if j.session_id == session_id
        ]:
            self._bg_jobs.pop(job_id, None)
            _LIVE_BACKGROUND_PGIDS.pop(job_id, None)
        return len(victims)

    # ── read_file ─────────────────────────────────────────────────

    async def _read_file(self, args: dict) -> dict:
        bad_args = _unexpected_args(args, {"path", "offset", "limit"}, "read_file")
        if bad_args:
            return bad_args

        path = Path(args.get("path", "")).expanduser()

        def _classify() -> "tuple[dict | None, str, bytes, int]":
            """Permission check + stat + magic sniff, in one thread hop.

            Returns ``(error, kind, head_bytes, size)`` where ``kind`` is
            ``"text"``, ``"image"`` or ``"binary"``. Kept separate from
            the text read below so an image never goes near
            ``read_text``: that is what produced line-numbered mojibake
            (`' 1|\\x89PNG'`) and reported it as a 200.
            """
            denied = self._check_read(str(path))
            if denied:
                return denied, "", b"", 0
            if not path.exists():
                return {"success": False, "status_code": 404, "data": None, "error": f"File not found: {path}"}, "", b"", 0
            if not path.is_file():
                return {"success": False, "status_code": 400, "data": None, "error": f"Not a file: {path}"}, "", b"", 0
            size = path.stat().st_size
            with path.open("rb") as fh:
                head = fh.read(8192)
            if _sniff_image_media_type(head) is not None:
                return None, "image", head, size
            # git's rule: a NUL byte in the first 8 KB means binary. It
            # keeps every real source file (including latin-1 and other
            # single-byte encodings) on the text path, where the existing
            # errors="replace" read has always handled them.
            if b"\x00" in head:
                return None, "binary", head, size
            return None, "text", head, size

        error, kind, head, size = await asyncio.to_thread(_classify)
        if error is not None:
            return error

        if kind == "image":
            return await self._read_image(path, head, size, args)
        if kind == "binary":
            return {
                "success": False,
                "status_code": 415,
                "data": {
                    "path": str(path),
                    "detected": _sniff_binary_kind(head),
                    "size_bytes": size,
                    "content_kind": "binary",
                },
                "error": (
                    f"{path} is {_sniff_binary_kind(head)}, not text and not an "
                    f"image format this tool can deliver (png, jpeg, gif, webp, "
                    f"bmp). Reading it as text would return meaningless "
                    f"replacement characters. Use coding_tools__bash with a "
                    f"format-aware tool (file, xxd, strings, pdftotext, unzip -l)."
                ),
            }

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
            if path.stat().st_size > MAX_TEXT_READ_BYTES:
                return {
                    "success": False, "status_code": 413,
                    "data": {
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                        "max_bytes": MAX_TEXT_READ_BYTES,
                    },
                    "error": (
                        f"File too large to read as text: {path.stat().st_size} bytes > "
                        f"{MAX_TEXT_READ_BYTES} byte limit. Read a window with "
                        f"offset/limit, or narrow with coding_tools__grep_search."
                    ),
                }, "", None
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

    async def _read_image(self, path: Path, head: bytes, size: int, args: dict) -> dict:
        """Return an image in the shape the tool-result image pipeline reads.

        ``image_data`` carries a complete ``data:<media_type>;base64,...``
        URL. That key is in ``agents.multimodal_blocks``'
        ``TOOL_RESULT_IMAGE_FIELDS`` and a data URL is matched by its
        ``_DATA_URL_RE`` at ANY size, so a 20x20 icon is delivered as a
        real image block exactly like a full screenshot. (The bare-base64
        recognition path there has a 512-char floor, which a tiny PNG
        would fall under, hence the data URL rather than a raw payload.)
        """
        media_type = _sniff_image_media_type(head) or "image/png"

        for name in ("offset", "limit"):
            if args.get(name) is not None:
                return {
                    "success": False, "status_code": 400,
                    "data": {"path": str(path), "content_kind": "image"},
                    "error": (
                        f"{name} is a line window and does not apply to an "
                        f"image ({media_type}). Re-read without it; the image "
                        f"is always delivered whole or not at all."
                    ),
                }

        ceiling = _image_byte_ceiling()
        if size > ceiling:
            return {
                "success": False,
                "status_code": 413,
                "data": {
                    "path": str(path),
                    "content_kind": "image",
                    "media_type": media_type,
                    "size_bytes": size,
                    "max_bytes": ceiling,
                },
                "error": (
                    f"Image is {size} bytes; the maximum this tool can deliver "
                    f"is {ceiling} bytes, set by the image pipeline's base64 "
                    f"budget (FERAL_TOOL_IMAGE_MAX_B64_CHARS). An image is "
                    f"never truncated, so it is refused whole. Resize it first "
                    f"(e.g. coding_tools__bash with sips or magick)."
                ),
            }

        raw = await asyncio.to_thread(path.read_bytes)
        encoded = base64.b64encode(raw).decode("ascii")
        data = {
            "path": str(path),
            "content_kind": "image",
            "media_type": media_type,
            "format": media_type.split("/", 1)[1],
            "size_bytes": size,
            "image_data": f"data:{media_type};base64,{encoded}",
        }
        dims = _image_dimensions(media_type, raw)
        if dims is not None:
            data["width"], data["height"] = dims
        return {"success": True, "status_code": 200, "data": data, "error": None}

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
        bad_args = _unexpected_args(args, GREP_ALLOWED_ARGS, "grep_search")
        if bad_args:
            return bad_args

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
        if output_mode not in ("files_with_matches", "content", "count"):
            return {
                "success": False, "status_code": 400, "data": None,
                "error": (
                    f"output_mode={output_mode!r} is not supported. Use "
                    f"'files_with_matches', 'content' or 'count'."
                ),
            }

        opts, opt_error = _grep_options(args, output_mode)
        if opt_error is not None:
            return opt_error

        denied = self._check_read(search_path)
        if denied:
            return denied

        wants_context = bool(opts["after"] or opts["before"])
        cmd = ["rg", "--color=never"]
        if opts["ignore_case"]:
            cmd.append("--ignore-case")
        if opts["multiline"]:
            cmd += ["--multiline", "--multiline-dotall"]
        if opts["file_type"]:
            cmd += ["--type", opts["file_type"]]
        if output_mode == "files_with_matches":
            cmd += ["--files-with-matches"]
        elif output_mode == "count":
            cmd += ["--count"]
        else:  # content
            cmd += ["--line-number", "--no-heading"]
            if wants_context:
                cmd += ["--after-context", str(opts["after"])]
                cmd += ["--before-context", str(opts["before"])]
                # Unambiguous field separators. rg's defaults (':' for a
                # match row, '-' for a context row) are also legal path
                # characters, so with context on, `a-b:1:x` cannot be
                # parsed back reliably. These control characters can not
                # appear in a path.
                cmd += [f"--field-match-separator={GREP_MATCH_SEP}"]
                cmd += [f"--field-context-separator={GREP_CONTEXT_SEP}"]
        if include:
            cmd += ["--glob", include]
        cmd += ["--", pattern, search_path]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=15)
        except FileNotFoundError:
            return await self._grep_fallback(
                pattern, search_path, include, output_mode, head_limit, offset, opts,
            )
        except asyncio.TimeoutError:
            return {"success": False, "status_code": 408, "data": None, "error": "Search timed out"}

        # rg exits 1 for "no matches" (not an error) and 2 for a real
        # failure, a bad pattern, an unknown --type, an unreadable path.
        # Returning an empty success for exit 2 is the same class of lie
        # as dropping a flag.
        if proc.returncode not in (0, 1):
            stderr = stderr_b.decode(errors="replace").strip()[:2000]
            return {
                "success": False, "status_code": 400,
                "data": {"ripgrep_exit_code": proc.returncode},
                "error": f"ripgrep failed: {stderr or 'unknown error'}",
            }

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
        elif wants_context:
            rows = [_parse_context_row(line) for line in window]
            matches = [row for row in rows if row is not None]
            data = {"mode": "content", "matches": matches, "total": total}
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
        _annotate_options(data, opts, engine="ripgrep")
        return {"success": True, "status_code": 200, "data": data, "error": None}

    async def _grep_fallback(
        self,
        pattern: str,
        search_path: str,
        include: str,
        output_mode: str = "files_with_matches",
        head_limit: int = GREP_DEFAULT_HEAD_LIMIT,
        offset: int = 0,
        opts: "dict | None" = None,
    ) -> dict:
        """Pure-Python fallback when ripgrep is not installed.

        Honors the same output_mode, pagination, case-insensitivity,
        context-line and multiline contract as the rg path. The one
        documented divergence is ``type``: ripgrep knows ~800 file types,
        this path knows the subset in ``FALLBACK_TYPE_GLOBS`` and REFUSES
        (400) anything outside it rather than returning results filtered
        by a different rule than the caller asked for.
        """
        opts = opts or _DEFAULT_GREP_OPTS
        # The pattern is model-authored, and Python's `re` backtracks, so
        # a catastrophic pattern hangs the brain with no timeout and no
        # way to interrupt it. Verified: `(a+)+$` against 30 characters
        # does not return. The ripgrep path above needs no guard because
        # Rust's regex engine is linear-time by construction; only this
        # fallback is exposed.
        flags = 0
        if opts["ignore_case"]:
            flags |= re.IGNORECASE
        if opts["multiline"]:
            flags |= re.DOTALL | re.MULTILINE
        try:
            regex = compile_safe_regex(pattern, flags)
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

        type_suffixes: "tuple[str, ...] | None" = None
        if opts["file_type"]:
            mapped = FALLBACK_TYPE_GLOBS.get(opts["file_type"])
            if mapped is None:
                return {
                    "success": False, "status_code": 400,
                    "data": {
                        "requested_type": opts["file_type"],
                        "supported_types": sorted(FALLBACK_TYPE_GLOBS),
                    },
                    "error": (
                        f"ripgrep is not installed, and the pure-Python "
                        f"fallback does not know the file type "
                        f"{opts['file_type']!r}. Known types: "
                        f"{', '.join(sorted(FALLBACK_TYPE_GLOBS))}. Use the "
                        f"`include` glob instead, or install ripgrep."
                    ),
                }
            type_suffixes = mapped

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
            if type_suffixes is not None and fp.suffix.lower() not in type_suffixes:
                continue
            if not fp.is_file() or fp.stat().st_size > 1_000_000:
                continue
            try:
                text = fp.read_text(errors="replace")
            except (PermissionError, OSError):
                continue
            rows = _fallback_rows_for_file(fp, text, regex, opts)
            if rows:
                files_with.append(_relativize(fp))
                content_rows.extend(rows)
            if len(content_rows) >= scan_cap:
                break

        if output_mode == "files_with_matches":
            window, truncated = _paginate(files_with, head_limit, offset)
            data: Dict[str, Any] = {"mode": "files_with_matches", "files": window, "total_files": len(files_with)}
        elif output_mode == "count":
            window, truncated = _paginate(files_with, head_limit, offset)
            per: Dict[str, int] = {}
            for r in content_rows:
                if r.get("is_context"):
                    continue
                per[r["file"]] = per.get(r["file"], 0) + 1
            data = {"mode": "count", "counts": [{"file": f, "count": str(per.get(f, 0))} for f in window], "total_files": len(files_with)}
        else:
            window, truncated = _paginate(content_rows, head_limit, offset)
            data = {"mode": "content", "matches": window, "total": len(content_rows)}

        if truncated:
            data["truncated"] = True
            data["pagination"] = {"limit": head_limit, "offset": offset, "next_offset": offset + len(window)}
        _annotate_options(data, opts, engine="python-fallback")
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
