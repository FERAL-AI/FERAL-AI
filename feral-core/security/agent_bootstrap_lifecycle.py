"""Actual reviewed encrypted-memory continuation adapter for BrainState.

No vault initialization, OS key access or provider calls occur on constructing,
reviewing or reading this controller. Dispatch deliberately runs the real init.
"""

import asyncio
import inspect
import json
import os

from security.agent_bootstrap_continuation import (
    AgentBootstrapContinuation,
    BootstrapAdapter,
    BootstrapRefusal,
)


def create_agent_bootstrap_controller(state, start_hooks):
    captured = {}

    def revision():
        # Hashing happens inside the controller; never return these values to UI.
        # Cover all effective process environment: constructors read more than
        # the LLM settings. Unserializable config refuses review, not coercion.
        settings = state.config.settings
        phone = settings.get("phone_bridge_url") or os.environ.get(
            "FERAL_PHONE_BRIDGE_URL", ""
        )
        if phone == "auto":
            raise BootstrapRefusal("bootstrap_config_requires_update")
        llm = settings.get("llm", {})
        if not isinstance(llm, dict):
            raise BootstrapRefusal("bootstrap_config_requires_update")
        from providers.model_classes import classify

        model = llm.get("model") or os.environ.get("FERAL_LLM_MODEL", "")
        if model and classify(
            llm.get("provider") or os.environ.get("FERAL_LLM_PROVIDER", "ollama"), model
        ) not in {"chat", "reasoning", "unknown"}:
            raise BootstrapRefusal("bootstrap_config_requires_update")
        vault_data = state.vault_coordinator.require_ready()._data
        if not isinstance(vault_data, dict):
            raise RuntimeError("Authenticated vault data is unavailable")
        # OAuth constructor prunes expired pending records. Require deliberate
        # resolution before this immutable startup review, never silently prune.
        credentials = vault_data.get("credentials", {})
        if not isinstance(credentials, dict) or any(
            isinstance(name, str) and name.startswith("oauth_pending_")
            for name in credentials
        ):
            raise BootstrapRefusal("bootstrap_config_requires_update")
        return json.dumps(
            {
                "vault": vault_data,
                "settings": state.config.settings,
                "credentials": state.config.credentials,
                "environment": dict(os.environ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def fence():
        if not state._native_bootstrap_required or state.memory is None:
            raise RuntimeError("Continuation is no longer pending")
        captured["attributes"] = dict(vars(state))
        captured["tasks"] = set(state._background_tasks)
        captured["report_start"] = len(state._boot_report.subsystems)
        state._native_agent_hooks_complete = False
        state._native_continuation_resources = []

    async def run(checkpoint):
        state._native_continuation_checkpoint = checkpoint
        try:
            checkpoint()
            await state.init()
            checkpoint()
            required = {
                "SkillRegistry",
                "NodeSubdeviceStore",
                "ProviderCatalog",
                "LLMProvider",
                "Orchestrator",
            }
            reports = state._boot_report.subsystems[captured["report_start"] :]
            latest = {record.name: record for record in reports if not record.optional}
            if any(
                name not in latest or latest[name].status.value != "ok"
                for name in required
            ):
                raise RuntimeError("Required bootstrap subsystems did not complete")
            await start_hooks(state, checkpoint)
            checkpoint()
        finally:
            state._native_continuation_checkpoint = None

    def commit():
        if (
            state.memory is None
            or state.orchestrator is None
            or not state._native_agent_hooks_complete
        ):
            raise RuntimeError("Incomplete continuation")
        from security.agent_turn_lease import attach_agent_dispatch_lease

        attach_agent_dispatch_lease(state)
        from api.boot_report import SubsystemStatus

        for report in reversed(state._boot_report.subsystems):
            if report.name == "EncryptedMemory":
                report.status = SubsystemStatus.OK
                report.message = "Authenticated memory restored; reviewed agent continuation completed."
                report.verified = True
                break
        state._native_bootstrap_required = False

    async def cleanup():
        # Do not touch the preexisting server heartbeat/probe registry. Cancel
        # only continuation-created tasks; retain their handles even when their
        # auto-discard callbacks run during cancellation.
        failures = []
        from security.agent_turn_lease import invalidate_agent_turns, drain_agent_turns

        queued_turns = getattr(state, "_native_pending_agent_turns", [])
        queued_turns.extend(invalidate_agent_turns(state))
        captured_turns = tuple(queued_turns)
        if not await drain_agent_turns(captured_turns, timeout=5):
            failures.append("agent-turns")
        else:
            queued_turns[:] = [
                task for task in queued_turns if task not in captured_turns
            ]
        tasks = set(state._background_tasks) - captured.get("tasks", set())
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            if pending:
                failures.append("tasks")
        old = captured.get("attributes", {})
        changed = [
            (name, value)
            for name, value in vars(state).copy().items()
            if value is not None and old.get(name) is not value
        ]
        seen = set()

        async def stop(value, methods):
            if value is None or id(value) in seen:
                return
            seen.add(id(value))
            for method in methods:
                callback = getattr(value, method, None)
                if callable(callback):
                    try:
                        if inspect.iscoroutinefunction(callback):
                            await asyncio.wait_for(callback(), timeout=5)
                        else:
                            result = await asyncio.wait_for(
                                asyncio.to_thread(callback), timeout=5
                            )
                            if inspect.isawaitable(result):
                                await asyncio.wait_for(result, timeout=5)
                    except BaseException:
                        failures.append("resource")
                    return

        orch = getattr(state, "orchestrator", None)
        if orch is not old.get("orchestrator"):
            callback = getattr(orch, "stop_consolidation_scheduler", None)
            if callable(callback):
                try:
                    await asyncio.wait_for(callback(), timeout=5)
                except BaseException:
                    failures.append("scheduler")
        # Producers first, clients afterwards. Do not close restored memory
        # while those producers are still running. Coordinator lock closes it.
        priority = {
            "cron_service": 0,
            "proactive": 0,
            "screen_loop": 0,
            "channel_manager": 0,
            "mqtt_bridge": 0,
            "email_watcher": 0,
            "memory_decay": 0,
            "taskflows": 0,
            "sync_scheduler": 0,
            "hardware_mesh": 0,
            "mcp_client": 1,
        }
        for name, value in sorted(changed, key=lambda pair: priority.get(pair[0], 2)):
            if name in (
                "memory",
                "vault",
                "vault_coordinator",
                "config",
            ) or name.startswith(("_native_", "_agent_")):
                continue
            await stop(
                value,
                (
                    "close_reviewed",
                    "stop_all",
                    "stop",
                    "disconnect_all",
                    "aclose",
                    "close",
                ),
            )
        if orch is not old.get("orchestrator"):
            await stop(getattr(orch, "llm", None), ("close", "aclose"))
        for device in getattr(state, "_native_continuation_resources", []) + getattr(
            state, "_brain_local_devices", []
        ):
            if device not in old.get("_brain_local_devices", []):
                await stop(device, ("disconnect", "close"))
        try:
            from services.mdns import stop_advertisement

            stop_advertisement()
        except Exception:
            failures.append("discovery")
        state._native_agent_hooks_complete = False
        state.orchestrator = None  # No partially published agent can be used.
        await state.vault_coordinator.lock()
        # If any producer failed to drain, keep its MemoryStore alive but
        # unreachable through state.memory until process exit. Closing it under
        # a still-live producer would create a second, avoidable failure.
        retired = getattr(state, "_native_pending_memory_close", [])
        late_turns = tuple(queued_turns)
        if not await drain_agent_turns(late_turns, timeout=5):
            failures.append("agent-turns")
        else:
            queued_turns[:] = [task for task in queued_turns if task not in late_turns]
        if not failures:
            closed = set()
            for memory in list(retired):
                if id(memory) in closed:
                    continue
                closed.add(id(memory))
                try:
                    memory.close()
                    retired.remove(memory)
                except Exception:
                    failures.append("memory")
        if failures:
            raise RuntimeError(
                "Partial services could not all be stopped; restart required"
            )

    adapter = BootstrapAdapter(
        revision,
        fence,
        run,
        commit,
        cleanup,
        lambda: state._native_agent_hooks_complete,
    )
    return AgentBootstrapContinuation(state, state.vault_coordinator, adapter)
