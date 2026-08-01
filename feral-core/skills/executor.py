"""
FERAL Skill Executor — Actually Calls the APIs
=================================================
The blind vault pattern: LLM outputs tool calls,
this executor injects auth and makes the HTTP requests.
The LLM NEVER sees API keys or OAuth tokens.
"""

from __future__ import annotations
import asyncio
import dataclasses
import os
import logging
import uuid
from typing import Optional

import httpx

from config.loader import feral_home
from models.skill_manifest import SkillManifest, SkillEndpoint
from security.exec_mode import MODE_DOCKER, MODE_REFUSED, NEEDS_DOCKER
from skills.result_budget import (
    DEFAULT_TIER,
    ResultBudget,
    TruncationReport,
    budget_for,
    clamp,
    get_budget,
)
from skills.sandbox_ports import SandboxPort, default_sandbox_port

logger = logging.getLogger("feral.executor")

# -A12: dangerous runners must not silently fall back to host execution.
# Keep a hardcoded fallback set so legacy manifests still get protected even
# before they add explicit `requires_sandbox: true`.
SANDBOX_REQUIRED_SKILL_IDS = {"workspace_scripts", "code_interpreter"}

# BlindVault namespace holding per-skill API keys.
#
# Skill ids are lowercase (``web_search``) and provider credentials are
# SCREAMING_CASE env-var names (``OPENAI_API_KEY``), so a flat namespace
# would *probably* not collide. "Probably" is not a storage contract: a
# skill legitimately called ``openai_api_key`` would silently read the
# operator's chat key. The namespaced vault API keeps the two apart, and
# ``_get_key`` still falls back to the flat namespace so keys written by
# older builds keep resolving.
SKILL_KEY_NAMESPACE = "skill_keys"


class SkillExecutor:
    """
    Executes tool calls against skill endpoints.
    
    Security model (blind vault):
    1. LLM outputs: {tool: "weather_current__current", args: {q: "London"}}
    2. Executor looks up the skill manifest
    3. Executor pulls auth credentials from local vault (env vars for now)
    4. Executor makes the HTTP request with injected auth headers
    5. Executor sanitizes the response and returns it to the LLM
    6. LLM NEVER sees the raw API key
    """

    def __init__(
        self,
        daemons: dict = None,
        *,
        sandbox_port: Optional[SandboxPort] = None,
    ):
        self.client = httpx.AsyncClient(timeout=15.0)
        self._daemons = daemons or {}
        self._daemon_types: dict[str, str] = {}
        self._vault: dict[str, str] = {}
        self._blind_vault = None
        self._pending_results: dict[str, asyncio.Future] = {}
        self._wasm_sandbox = None
        # -A14: prefer narrow facade over a direct ``api.state`` import so
        # tests can inject and so global-state coupling shrinks.
        self._sandbox_port: SandboxPort = sandbox_port or default_sandbox_port()

    def set_sandbox_port(self, port: SandboxPort) -> None:
        """Replace the sandbox facade (used for tests/embedding scenarios)."""
        self._sandbox_port = port

    def load_vault_from_env(self):
        """Load API keys from environment variables."""
        for key, value in os.environ.items():
            if key.startswith("FERAL_KEY_"):
                skill_id = key[len("FERAL_KEY_"):]
                self._vault[skill_id] = value
                logger.info(f"Vault: loaded key for skill '{skill_id}'")

    def set_key(self, skill_id: str, key: str):
        """Manually set an API key for a skill (process memory only)."""
        self._vault[skill_id] = key

    def store_key(self, skill_id: str, key: str) -> bool:
        """Persist a skill API key and make it usable immediately.

        This is the write half of the round trip the operator surfaces
        (``POST /api/config/credentials`` with ``skill_keys``, and
        ``POST /api/skills/{skill_id}/key``) need. Before this existed
        the routes wrote into ``ConfigLoader._credentials["skill_keys"]``,
        which nothing on the execution path ever read: the ONLY way to
        give a skill a key was to export ``FERAL_KEY_<skill_id>`` and
        restart the brain.

        Writes to the encrypted vault first (survives a restart) and to
        the in-memory cache always (no restart needed). Returns True
        when the value reached the vault.

        Raises ``ValueError`` for an empty ``skill_id`` / ``key`` so a
        route cannot store a blank credential and report success.
        """
        skill_id = (skill_id or "").strip()
        key = (key or "").strip()
        if not skill_id:
            raise ValueError("skill_id is required")
        if not key:
            raise ValueError("key is required")

        self._vault[skill_id] = key
        persisted = False
        if self._blind_vault is not None and hasattr(self._blind_vault, "put"):
            try:
                self._blind_vault.put(
                    SKILL_KEY_NAMESPACE, skill_id, key, stored_by="skill_key_api",
                )
                persisted = True
            except Exception as exc:
                logger.warning(
                    "store_key: vault write failed for skill '%s': %s, the key "
                    "is live in this process but will not survive a restart.",
                    skill_id, exc,
                )
        else:
            logger.warning(
                "store_key: no vault attached; skill '%s' key is live in this "
                "process only and will not survive a restart.", skill_id,
            )
        return persisted

    def remove_key(self, skill_id: str) -> bool:
        """Forget a skill API key (memory + vault). True when something went."""
        skill_id = (skill_id or "").strip()
        if not skill_id:
            return False
        removed = self._vault.pop(skill_id, None) is not None
        vault = self._blind_vault
        if vault is not None:
            for remover, args in (
                (getattr(vault, "remove_from", None), (SKILL_KEY_NAMESPACE, skill_id)),
                (getattr(vault, "remove", None), (skill_id,)),
            ):
                if remover is None:
                    continue
                try:
                    removed = bool(remover(*args)) or removed
                except Exception as exc:
                    logger.warning(
                        "remove_key: vault delete failed for '%s': %s", skill_id, exc,
                    )
        return removed

    def key_ids(self) -> list[str]:
        """Skill ids that currently have a key, for status surfaces.

        Never returns the values, the UI only needs to know which rows
        are configured.
        """
        ids = set(self._vault.keys())
        vault = self._blind_vault
        lister = getattr(vault, "list_namespace", None) if vault is not None else None
        if lister is not None:
            try:
                ids.update(lister(SKILL_KEY_NAMESPACE))
            except Exception as exc:
                logger.debug("key_ids: vault listing failed: %s", exc)
        return sorted(ids)

    def has_key(self, skill_id: str) -> bool:
        """True when :meth:`_get_key` would resolve a key for this skill."""
        return bool(self._get_key(skill_id))

    def set_blind_vault(self, vault):
        """Connect the BlindVault for secure credential retrieval."""
        self._blind_vault = vault

    def set_wasm_sandbox(self, sandbox):
        """Connect the WASM sandbox for skill execution."""
        self._wasm_sandbox = sandbox

    @staticmethod
    def _is_sandbox_required(skill: SkillManifest, endpoint: SkillEndpoint) -> bool:
        if bool(getattr(endpoint, "requires_sandbox", False)):
            return True
        if bool(getattr(skill, "requires_sandbox", False)):
            return True
        return str(getattr(skill, "skill_id", "") or "") in SANDBOX_REQUIRED_SKILL_IDS

    def _sandbox_requirement_status(
        self,
        skill: SkillManifest,
        endpoint: SkillEndpoint,
    ) -> tuple[bool, str]:
        runtime = getattr(skill, "runtime", None) or getattr(endpoint, "runtime", None)
        if runtime == "wasm":
            wasm = self._wasm_sandbox
            if wasm is None:
                # -A14: support the narrow facade for WASM too so callers that
                # inject ``SandboxPort`` can fully avoid global state.
                try:
                    wasm = self._sandbox_port.get_wasm_sandbox()
                except Exception:
                    wasm = None
            if not wasm:
                return False, "WASM sandbox not configured"
            available_attr = getattr(wasm, "available", False)
            available = available_attr() if callable(available_attr) else bool(available_attr)
            if not available:
                return False, "WASM sandbox unavailable"
            return True, "ok"

        # Default strict boundary is Docker sandbox for host-code runners.
        try:
            docker_sandbox = self._sandbox_port.get_docker_sandbox()
        except Exception as exc:
            return False, f"sandbox port unavailable: {exc}"

        if docker_sandbox is None:
            return False, "Docker sandbox unavailable"

        try:
            available_attr = getattr(docker_sandbox, "available", None)
            available = (
                bool(available_attr())
                if callable(available_attr)
                else bool(available_attr)
            )
        except Exception as exc:
            return False, f"Docker sandbox health check failed: {exc}"

        if not available:
            return False, "Docker sandbox is not healthy"
        return True, "ok"

    def _get_key(self, skill_id: str) -> Optional[str]:
        """Resolve a skill's API key.

        Order, most specific first:

        1. The vault's ``skill_keys`` namespace, what the Settings UI
           writes through :meth:`store_key`.
        2. The vault's flat default namespace, where builds before this
           change put anything a caller passed to ``vault.store``.
        3. The in-process cache, populated by ``FERAL_KEY_<SKILL_ID>``
           env vars at construction and by :meth:`set_key` /
           :meth:`store_key`.
        """
        skill_id = (skill_id or "").strip()
        if not skill_id:
            return None
        vault = self._blind_vault
        if vault is not None:
            getter = getattr(vault, "get", None)
            if getter is not None:
                try:
                    key = getter(
                        SKILL_KEY_NAMESPACE, skill_id, requester="executor",
                    )
                except Exception as exc:
                    logger.debug("_get_key: namespaced vault read failed: %s", exc)
                else:
                    if key:
                        return key
            try:
                key = vault.retrieve(skill_id, requester="executor")
            except Exception as exc:
                logger.debug("_get_key: flat vault read failed: %s", exc)
            else:
                if key:
                    return key
        return self._vault.get(skill_id)

    async def execute(
        self,
        tool_name: str,
        args: dict,
        skill: SkillManifest,
        endpoint: SkillEndpoint,
    ) -> dict:
        """
        Execute a skill endpoint call.
        """
        logger.info(f"Executing: {tool_name} → {endpoint.method} {endpoint.url}")
        logger.info(f"  args: {args}")

        from observability.metrics import increment, observe
        import time as _time
        increment("feral.skill.invocations_total", attributes={"skill": skill.skill_id, "endpoint": endpoint.id})
        _t0 = _time.time()
        try:
            return await self._execute_inner(tool_name, args, skill, endpoint)
        finally:
            observe("feral.skill.exec_latency_ms", (_time.time() - _t0) * 1000,
                    {"skill": skill.skill_id, "endpoint": endpoint.id})

    async def _execute_inner(
        self, tool_name: str, args: dict, skill: SkillManifest, endpoint: SkillEndpoint,
    ) -> dict:
        sandbox_required = self._is_sandbox_required(skill, endpoint)
        if sandbox_required:
            sandbox_ok, sandbox_reason = self._sandbox_requirement_status(skill, endpoint)
            if not sandbox_ok:
                # Name the execution mode that was required. A bare "sandbox
                # unavailable" 503 left the operator with nothing to act on;
                # ``needs`` distinguishes "start Docker" from "grant a folder"
                # (the workspace-scoped host mode in security/exec_mode.py).
                msg = (
                    f"Sandbox required for '{skill.skill_id}__{endpoint.id}' "
                    f"but unavailable: {sandbox_reason}. This endpoint runs "
                    f"generated code, so the '{MODE_DOCKER}' execution mode is "
                    f"mandatory (a workspace grant does not substitute)."
                )
                logger.warning(msg)
                return {
                    "success": False,
                    "status_code": 503,
                    "data": {
                        "execution_mode": MODE_REFUSED,
                        "needs": NEEDS_DOCKER,
                        "required_mode": MODE_DOCKER,
                    },
                    "error": msg,
                }

        # 1. Check if there's a Python implementation backing this skill
        from skills.impl import get_implementation
        impl = get_implementation(skill.skill_id)
        if impl:
            logger.info(f"Executing via Python backing class: {impl.__class__.__name__}")
            # -A14: keep backing implementations on the same sandbox facade
            # as the executor (avoids split-brain in tests/embedding).
            if hasattr(impl, "set_sandbox_port"):
                try:
                    impl.set_sandbox_port(self._sandbox_port)
                except Exception as exc:
                    logger.debug("impl sandbox-port injection skipped: %s", exc)
            exec_args = dict(args or {})
            if sandbox_required:
                # Let backing implementations know host fallback is forbidden.
                exec_args["_feral_require_sandbox"] = True
            try:
                result = await impl.execute(endpoint.id, exec_args, self._vault)
                # The budget is declared by the manifest for THIS endpoint
                # (see skills/result_budget.py). Before v2026.7.30 a single
                # global 2 000-char / 20-item clamp ran here, which meant
                # coding_tools__read_file returned ~30 lines of source no
                # matter what `limit` the model passed and grep_search
                # returned 20 of the 250 matches it had already computed.
                budget = budget_for(skill.skill_id, endpoint.id, skill)
                if isinstance(result, dict) and "success" in result:
                    payload, note = self._sanitize_with_note(result.get("data"), budget)
                    envelope = {
                        "success": result["success"],
                        "status_code": result.get("status_code", 200),
                        "data": payload,
                        "error": result.get("error"),
                    }
                else:
                    payload, note = self._sanitize_with_note(result, budget)
                    envelope = {
                        "success": True,
                        "status_code": 200,
                        "data": payload,
                        "error": None,
                    }
                if note:
                    # Say so out loud. A silent cut is the worst outcome:
                    # the model assumes it read the whole file and answers
                    # confidently about code it never saw.
                    envelope["_truncation_note"] = note
                return envelope
            except Exception as e:
                logger.error(f"Python Skill error: {e}", exc_info=True)
                return {"success": False, "status_code": 500, "data": None, "error": str(e)}

        # 2. WASM runtime — execute in sandbox
        runtime = getattr(skill, 'runtime', None) or getattr(endpoint, 'runtime', None)
        if runtime == "wasm" and self._wasm_sandbox and self._wasm_sandbox.available:
            return await self._execute_via_wasm(tool_name, endpoint, args, skill)

        # 3. WS_EXECUTE — route to a connected daemon via WebSocket
        if endpoint.method == "WS_EXECUTE":
            return await self._execute_via_daemon(tool_name, endpoint, args, skill)

        # 4. daemon:// protocol — local shell/AppleScript execution
        if endpoint.url.startswith("daemon://"):
            path = endpoint.url.replace("daemon://local/", "")
            command = args.get("script") or args.get("command") or ""
            return await self._execute_local_daemon(path, command)

        # 5. Fallback to standard HTTP generic JSON runner
        url = endpoint.url
        method = endpoint.method.upper()
        headers = {}

        # Inject auth from vault (BlindVault → env cache fallback)
        auth_query_params = {}
        api_key = self._get_key(skill.skill_id)
        if skill.auth.type == "api_key" and api_key:
            header_name = skill.auth.api_key_header or "Authorization"
            # Some APIs (e.g. OpenWeather) use query params instead of headers
            if header_name.islower() and "-" not in header_name:
                auth_query_params[header_name] = api_key
            else:
                headers[header_name] = api_key
        elif skill.auth.type == "bearer" and api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # URL parameter substitution (e.g., /restaurants/{id}/menu)
        for param_name, param_value in args.items():
            placeholder = f"{{{param_name}}}"
            if placeholder in url:
                url = url.replace(placeholder, str(param_value))

        try:
            if method == "GET":
                query_params = {k: v for k, v in args.items() if f"{{{k}}}" not in endpoint.url}
                query_params.update(auth_query_params)
                resp = await self.client.get(url, params=query_params, headers=headers)
            elif method in ("POST", "PUT", "PATCH"):
                resp = await self.client.request(method, url, json=args, headers=headers)
            elif method == "DELETE":
                resp = await self.client.delete(url, headers=headers)
            else:
                return {"success": False, "status_code": 0, "data": None, "error": f"Unknown method: {method}"}

            # Parse response
            try:
                data = resp.json()
            except Exception:
                data = {"raw_text": resp.text[:1000]}

            # Sanitize — strip any potential prompt injection from response
            sanitized = self._sanitize_response(data)

            return {
                "success": 200 <= resp.status_code < 300,
                "status_code": resp.status_code,
                "data": sanitized,
                "error": None if resp.status_code < 400 else f"HTTP {resp.status_code}",
            }

        except httpx.TimeoutException:
            logger.error(f"Timeout calling {url}")
            return {"success": False, "status_code": 0, "data": None, "error": "Request timed out"}
        except Exception as e:
            logger.error(f"Error calling {url}: {e}")
            return {"success": False, "status_code": 0, "data": None, "error": str(e)}

    async def _execute_local_daemon(self, path: str, command: str) -> dict:
        """Execute daemon:// URLs as local shell or AppleScript commands.

        Shell execution is gated by ``SandboxPolicy.validate_shell_command``:
        argv[0] must be on the daemon shell allowlist, shell metacharacters
        are rejected outright, and ``execution.allow_shell_commands`` must
        be enabled. We always exec with ``shell=False`` on the parsed argv
        list — no free-form string ever reaches an interpreter.

        AppleScript execution is gated by
        ``SandboxPolicy.validate_applescript``. Before that landed, this
        branch ran ``osascript -e <command>`` with no validation at all
        while its sibling ``shell`` branch below was fully validated, and
        since AppleScript can invoke a shell (``do shell script``),
        the unvalidated path defeated the validated one.
        """
        import subprocess
        import shlex

        from security.sandbox_policy import SandboxPolicy

        if not command:
            return {"success": False, "status_code": 400, "data": None, "error": "No command or script provided"}

        if path == "applescript":
            policy = SandboxPolicy.load_default()
            ok, reason = policy.validate_applescript(command)
            if not ok:
                return {"success": False, "status_code": 403, "data": None, "error": reason}
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    ["osascript", "-e", command],
                    capture_output=True, text=True, timeout=15,
                )
            except subprocess.TimeoutExpired:
                return {"success": False, "status_code": 0, "data": None, "error": "Command timed out (15s)"}
            except Exception as e:
                logger.error(f"Local daemon applescript failed: {e}")
                return {"success": False, "status_code": 0, "data": None, "error": str(e)}
        elif path == "shell":
            # audit-r14 / Lane 8 §A3 — replace the old substring blocklist
            # ("rm ", "sudo ", …) + ``shell=True`` runner with the declarative
            # policy validator. The validator enforces argv[0] allowlist and
            # rejects shell metacharacters; we then exec the parsed argv with
            # ``shell=False`` so no interpreter can re-expand the string.
            policy = SandboxPolicy.load_default()
            ok, reason = policy.validate_shell_command(command)
            if not ok:
                return {"success": False, "status_code": 403, "data": None, "error": reason}

            try:
                argv = shlex.split(command.strip(), posix=True)
            except ValueError as e:
                return {"success": False, "status_code": 400, "data": None, "error": f"Could not parse command: {e}"}

            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    argv,
                    shell=False,
                    capture_output=True, text=True, timeout=15,
                )
            except subprocess.TimeoutExpired:
                return {"success": False, "status_code": 0, "data": None, "error": "Command timed out (15s)"}
            except FileNotFoundError as e:
                return {"success": False, "status_code": 404, "data": None, "error": str(e)}
            except PermissionError as e:
                return {"success": False, "status_code": 403, "data": None, "error": str(e)}
            except Exception as e:
                logger.error(f"Local daemon shell failed: {e}")
                return {"success": False, "status_code": 0, "data": None, "error": str(e)}
        else:
            return {"success": False, "status_code": 400, "data": None, "error": f"Unknown daemon path: {path}"}

        output = proc.stdout.strip() or proc.stderr.strip()
        return {
            "success": proc.returncode == 0,
            "status_code": 200 if proc.returncode == 0 else 500,
            "data": {"output": output, "exit_code": proc.returncode},
            "error": proc.stderr.strip() if proc.returncode != 0 else None,
        }

    async def _execute_via_wasm(
        self, tool_name: str, endpoint: SkillEndpoint, args: dict, skill: SkillManifest,
    ) -> dict:
        """Execute a skill via the WASM sandbox."""
        skills_dir = feral_home() / "skills" / skill.skill_id
        wasm_files = list(skills_dir.glob("*.wasm"))
        if not wasm_files:
            return {"success": False, "status_code": 404, "data": None, "error": f"No .wasm file found for {skill.skill_id}"}

        result = await self._wasm_sandbox.execute(
            wasm_path=str(wasm_files[0]),
            params=args,
            entry_point=endpoint.id,
        )

        return {
            "success": result.get("success", False),
            "status_code": 200 if result.get("success") else 500,
            "data": self._sanitize_response(result.get("data")),
            "error": result.get("error"),
        }

    def register_daemon_type(self, node_id: str, node_type: str):
        """Record the declared type of a connected daemon node."""
        self._daemon_types[node_id] = node_type.lower() if node_type else "unknown"

    def unregister_daemon(self, node_id: str):
        """Remove daemon type record when a node disconnects."""
        self._daemon_types.pop(node_id, None)

    async def _execute_via_daemon(
        self, tool_name: str, endpoint: SkillEndpoint, args: dict, skill: SkillManifest,
    ) -> dict:
        """Route a WS_EXECUTE skill call to the appropriate connected daemon with strict type matching."""
        target_type = (skill.daemon_node_type or "robot").lower()

        target_daemon = None
        target_ws = None

        if target_type == "any":
            for node_id, ws in self._daemons.items():
                target_daemon = node_id
                target_ws = ws
                break
        else:
            for node_id, ws in self._daemons.items():
                registered_type = self._daemon_types.get(node_id, "unknown")
                if registered_type == target_type or target_type in node_id.lower():
                    target_daemon = node_id
                    target_ws = ws
                    break

        if not target_ws:
            available = {nid: self._daemon_types.get(nid, "unknown") for nid in self._daemons}
            available_str = ", ".join(f"{nid} ({t})" for nid, t in available.items()) or "none"
            return {
                "success": False, "status_code": 503, "data": None,
                "error": f"No connected daemon of type '{target_type}' to execute {tool_name}. Available daemons: {available_str}",
            }

        request_id = str(uuid.uuid4())
        execute_msg = {
            "hop": "brain",
            "type": "execute",
            "msg_id": request_id,
            "payload": {
                "executor": endpoint.id,
                "args": args,
                "skill_id": skill.skill_id,
            },
        }

        future = asyncio.get_event_loop().create_future()
        self._pending_results[request_id] = future

        try:
            await target_ws.send_json(execute_msg)
            logger.info(f"WS_EXECUTE → {target_daemon}: {endpoint.id} (req={request_id})")

            result = await asyncio.wait_for(future, timeout=15.0)
            return {
                "success": result.get("status") == "success",
                "status_code": 200 if result.get("status") == "success" else 500,
                "data": self._sanitize_response(result.get("stdout") or result.get("data")),
                "error": result.get("error"),
            }

        except asyncio.TimeoutError:
            self._pending_results.pop(request_id, None)
            return {
                "success": False, "status_code": 504, "data": None,
                "error": f"Daemon {target_daemon} timed out executing {endpoint.id}",
            }
        except Exception as e:
            self._pending_results.pop(request_id, None)
            return {"success": False, "status_code": 500, "data": None, "error": str(e)}

    def resolve_daemon_result(self, request_id: str, result: dict):
        """Called when a daemon sends back an execute_result."""
        future = self._pending_results.pop(request_id, None)
        if future and not future.done():
            future.set_result(result)

    @staticmethod
    def _sanitize_with_note(data, budget: ResultBudget):
        """Bound ``data`` to ``budget`` and report what (if anything) was cut.

        Returns ``(payload, note)`` where ``note`` is "" when nothing was
        removed. Callers put the note on the result envelope, next to
        ``data`` rather than inside it, so the payload keeps the shape the
        skill's ``returns_description`` promised.
        """
        report = TruncationReport()
        payload = clamp(data, budget, report)
        return payload, (report.summary() if report else "")

    def _sanitize_response(
        self,
        data,
        max_depth: Optional[int] = None,
        max_str_len: Optional[int] = None,
        max_list_len: Optional[int] = None,
        budget: Optional[ResultBudget] = None,
    ) -> any:
        """
        Sanitize a skill response before feeding it back to the LLM.
        Prevents:
        - Prompt injection via response content
        - Excessive data that would overflow context
        - Unbounded / adversarial payload shapes (depth, dict width)

        The numbers now come from a ``ResultBudget`` (skills/result_budget.py)
        rather than from literals baked into this signature. Callers that
        pass no budget get ``DEFAULT_TIER`` — the historical 2 000-char /
        20-item bound — which is deliberately what every third-party HTTP
        response, WASM result and daemon payload keeps getting. Only skills
        whose built-in manifest declares a wider tier are widened, and the
        widening is per endpoint.

        The explicit ``max_*`` keyword arguments remain supported so a
        caller can pin one axis without constructing a budget.
        """
        effective = budget or get_budget(DEFAULT_TIER)
        overrides = {}
        if max_depth is not None:
            overrides["max_depth"] = max_depth
        if max_str_len is not None:
            overrides["max_str_len"] = max_str_len
        if max_list_len is not None:
            overrides["max_list_len"] = max_list_len
        if overrides:
            effective = dataclasses.replace(effective, **overrides)
        return clamp(data, effective)

    async def close(self):
        await self.client.aclose()
