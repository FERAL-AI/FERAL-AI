"""FERAL Desktop Control: backing implementation.

Why this module exists
======================
``desktop_control`` was a manifest-only skill: every endpoint carried a
``daemon://local/{shell,applescript}`` URL and ``SkillExecutor``
validated the free-form string the model wrote. That works for
"take a screenshot", and it fails for the request that started this
lane:

    "open a YouTube song on Chrome"

The model reads ``open_app``'s description, writes the natural
AppleScript (``tell application "Google Chrome" to open location
"https://…"``), and ``SandboxPolicy.validate_applescript`` refuses it,
because ``open location`` dispatches *any* URL scheme (``file://``,
``x-apple-shortcuts://``, a third-party app's private scheme), which
makes it an interpreter reached through a side door. The refusal is
correct. What was missing is the permitted path: ``open`` is on the
daemon shell allowlist and ``open -a "Google Chrome" <https url>`` was
always going to be accepted. Nothing told the model that.

``open_url`` is that path, and it needs structured parameters rather
than a command string for two reasons:

1. **Scheme enforcement.** A free-form ``command`` string reaching the
   allowlist would let ``open file:///Users/…/id_rsa`` and
   ``open x-apple-shortcuts://run-shortcut?name=…`` through, which is
   precisely the capability ``open location`` is denied for. This
   module parses the URL and accepts ``http``/``https`` only, with a
   real host, before an argv is built. See :func:`_validate_url`.
2. **Ampersands.** ``SandboxPolicy._SHELL_REJECT_CHARS`` contains
   ``&``, and the scan runs on the raw command string, so
   ``open "https://youtube.com/watch?v=X&t=30"`` is refused today even
   though the executor would exec argv with ``shell=False`` and no
   shell would ever see the character. Half of real URLs carry a second
   query parameter. Building the argv here instead of a string sidesteps
   a scan that has nothing to scan: there is no string to re-expand.

Because a Python backing implementation claims *every* endpoint of its
skill (``SkillExecutor._execute_inner`` looks the impl up by
``skill_id``), this module also has to serve the script/command
endpoints. It does not reimplement them: it builds the same
``(path, command)`` pair the manifest used to hand the executor and
calls ``SkillExecutor._execute_local_daemon`` directly, so
``validate_applescript`` / ``validate_shell_command`` run on exactly
the same code as before. ``tests/test_desktop_control_impl.py`` pins
that equivalence.

How ``open_url`` is constrained
===============================
* Scheme must be ``http`` or ``https``. Everything else (``file``,
  ``ftp``, ``javascript``, ``data``, ``facetime``, ``x-apple-...``, any
  app's private scheme) is refused before an argv exists.
* The URL must have a network host, so ``https:///etc/passwd`` and
  ``http://`` are refused.
* No whitespace, no control characters, no NUL, 2 048 characters max.
* ``app`` is an application *name*, matched against a conservative
  charset. It is never a path, never a bundle id with a scheme, and
  cannot begin with ``-`` (which would turn it into another ``open``
  flag such as ``--args`` or ``-e``).
* argv is built here, in this order, and can never grow another
  element: ``["open"] + ["-a", app]? + ["--", url]``. ``--`` stops
  ``open`` from reading a URL that starts with a dash as a flag.
* ``open`` is still checked against
  ``SandboxPolicy.daemon_shell_allowlist()`` and
  ``execution.allow_shell_commands`` is still honoured, so an operator
  who removes ``open`` from the allowlist disables this endpoint too.
* ``subprocess.run`` with a list and ``shell=False``. No interpreter.

The net effect is *narrower* than what the daemon shell allowlist
already permits: ``desktop_control__shell_command`` can run
``open file:///…`` today, and ``open_url`` cannot.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skill.desktop_control")

# Only these two. Named as a constant so the test that pins the refusal
# list and the description in the manifest have one thing to agree with.
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

MAX_URL_LENGTH = 2048

# Application names as a person writes them: "Safari", "Google Chrome",
# "Visual Studio Code", "IINA". Deliberately no "/" (a path), no ":" (a
# scheme or a bundle path), and a leading "-" is refused separately so
# the name cannot become an ``open`` flag.
_APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$")

# Endpoints that hand a script to ``daemon://local/applescript`` and
# endpoints that hand a command to ``daemon://local/shell``. The values
# are the daemon path; the arg the model fills is `script` for the
# first group and `command` for the second. Kept as data so
# ``tests/test_desktop_control_impl.py`` can assert the manifest and
# this table name the same endpoints.
APPLESCRIPT_ENDPOINTS: frozenset[str] = frozenset({
    "open_app",
    "quit_app",
    "list_running_apps",
    "lock_screen",
})

SHELL_ENDPOINTS: frozenset[str] = frozenset({
    "shell_command",
    "screenshot",
    "system_info",
    "set_volume",
    "disk_space",
})


def _refusal(reason: str, status: int = 400) -> dict:
    return {"success": False, "status_code": status, "data": None, "error": reason}


def _validate_url(raw: Any) -> tuple[Optional[str], str]:
    """Return ``(url, "")`` for an acceptable URL, else ``(None, reason)``.

    The reason string is written for the model: it says what was wrong
    and which tool to use instead, because a refusal the model cannot
    act on turns into "FERAL says it cannot".
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "`url` is required and must be a non-empty string."
    url = raw.strip()

    if len(url) > MAX_URL_LENGTH:
        return None, f"`url` exceeds the {MAX_URL_LENGTH}-character limit."
    if "\x00" in url:
        return None, "`url` contains a NUL byte."
    if any(ch.isspace() for ch in url):
        return None, (
            "`url` contains whitespace. Percent-encode spaces as %20 "
            "(e.g. https://www.youtube.com/results?search_query=lofi%20hip%20hop)."
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None, "`url` contains a control character."

    parsed = urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        return None, (
            "`url` must start with http:// or https:// "
            f"(got {url[:60]!r}). desktop_control__open_url opens web pages only."
        )
    if scheme not in ALLOWED_URL_SCHEMES:
        return None, (
            f"URL scheme {scheme!r} is not permitted. "
            f"desktop_control__open_url opens {'/'.join(sorted(ALLOWED_URL_SCHEMES))} "
            f"URLs only, never file://, never an application's private scheme. "
            f"To open a local folder or file use "
            f"desktop_control__shell_command with `open <path>`."
        )
    if not parsed.hostname:
        return None, (
            f"`url` has no host ({url[:60]!r}). "
            f"An http/https URL must name a site, e.g. https://www.youtube.com."
        )
    return url, ""


def _validate_app(raw: Any) -> tuple[Optional[str], str]:
    """Return ``(app_or_None, "")`` for an acceptable app name."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, ""
    if not isinstance(raw, str):
        return None, "`app` must be a string application name, e.g. \"Google Chrome\"."
    app = raw.strip()
    if app.startswith("-"):
        return None, (
            "`app` must not start with '-'; it is an application name "
            "(\"Safari\", \"Google Chrome\"), not an `open` flag."
        )
    if not _APP_NAME_RE.match(app):
        return None, (
            f"`app` {app!r} is not a plain application name. Use the name as it "
            f"appears in /Applications, e.g. \"Safari\", \"Google Chrome\", "
            f"\"Firefox\". Paths and bundle identifiers are not accepted."
        )
    return app, ""


@register_skill
class DesktopControlSkill(BaseSkill):
    """Backing implementation for the ``desktop_control`` manifest."""

    def __init__(self):
        super().__init__(skill_id="desktop_control")

    async def execute(
        self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str],
    ) -> Dict[str, Any]:
        dispatch = {
            "open_url": self._open_url,
            "open_app": self._applescript_endpoint,
            "quit_app": self._applescript_endpoint,
            "list_running_apps": self._applescript_endpoint,
            "lock_screen": self._applescript_endpoint,
            "shell_command": self._shell_endpoint,
            "screenshot": self._shell_endpoint,
            "system_info": self._shell_endpoint,
            "set_volume": self._shell_endpoint,
            "disk_space": self._shell_endpoint,
        }
        handler = dispatch.get(endpoint_id)
        if handler is None:
            return _refusal(f"Unknown endpoint: {endpoint_id}", status=404)
        try:
            return await handler(dict(args or {}))
        except Exception as exc:  # noqa: BLE001, never crash the orchestrator
            logger.exception("desktop_control.%s failed", endpoint_id)
            return _refusal(str(exc), status=500)

    # ── the script / command endpoints ────────────────────────────
    #
    # These are the endpoints that existed before ``open_url``. They
    # keep going through the executor's daemon lane so
    # ``validate_applescript`` / ``validate_shell_command`` run on the
    # one implementation everybody reviewed.

    async def _applescript_endpoint(self, args: dict) -> dict:
        script = (args.get("script") or args.get("command") or "").strip()
        if not script:
            return _refusal("No AppleScript provided in `script`.")
        return await self._run_daemon("applescript", script)

    async def _shell_endpoint(self, args: dict) -> dict:
        command = (args.get("command") or args.get("script") or "").strip()
        if not command:
            return _refusal("No command provided in `command`.")
        return await self._run_daemon("shell", command)

    @staticmethod
    async def _run_daemon(path: str, payload: str) -> dict:
        from skills.executor import SkillExecutor

        return await SkillExecutor._execute_local_daemon(path, payload)

    # ── open_url ──────────────────────────────────────────────────

    async def _open_url(self, args: dict) -> dict:
        url, reason = _validate_url(args.get("url"))
        if url is None:
            return _refusal(reason, status=400)

        app, reason = _validate_app(args.get("app"))
        if reason:
            return _refusal(reason, status=400)

        from security.sandbox_policy import SandboxPolicy

        policy = SandboxPolicy.load_default()
        allowlist = policy.daemon_shell_allowlist()
        if "open" not in allowlist:
            return _refusal(
                "The `open` program is not on the daemon shell allowlist "
                f"({', '.join(allowlist) or '(empty)'}), so this machine's "
                f"policy does not permit opening URLs.",
                status=403,
            )
        if not policy.can_execute_shell():
            return _refusal(
                "Local program execution is disabled by policy "
                "(execution.allow_shell_commands=false).",
                status=403,
            )

        argv = ["open"]
        if app:
            argv += ["-a", app]
        # ``--`` so a URL is never re-read as an ``open`` flag.
        argv += ["--", url]

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return _refusal("open timed out (15s)", status=504)
        except FileNotFoundError as exc:
            return _refusal(str(exc), status=404)
        except PermissionError as exc:
            return _refusal(str(exc), status=403)

        output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        if proc.returncode != 0:
            hint = ""
            if app:
                hint = (
                    f" If '{app}' is not installed, retry without `app` to use "
                    f"the default browser."
                )
            return {
                "success": False,
                "status_code": 500,
                "data": {"url": url, "app": app, "exit_code": proc.returncode},
                "error": (output or f"open exited {proc.returncode}") + hint,
            }
        return {
            "success": True,
            "status_code": 200,
            "data": {
                "url": url,
                "app": app,
                "output": output,
                "exit_code": proc.returncode,
            },
            "error": None,
        }


__all__ = [
    "ALLOWED_URL_SCHEMES",
    "APPLESCRIPT_ENDPOINTS",
    "DesktopControlSkill",
    "SHELL_ENDPOINTS",
]
