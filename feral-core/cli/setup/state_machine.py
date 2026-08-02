"""Tiny state machine that drives the linear wizard flow with back/quit.

audit-r14 / lane-07  — adds resume support. After every successful
step the machine calls :meth:`WizardState.write_setup_state` so a
crash / quit mid-flow leaves a sidecar at
``~/.feral/setup_state.json``. The next ``feral setup`` invocation
reads the sidecar and (when one exists) starts at the last completed
step instead of forcing the operator to re-walk the whole wizard.
"""

from __future__ import annotations

import inspect
import logging
from typing import Awaitable, Callable, Sequence

from cli import ui_kit

from .helpers import (
    BackNavigation,
    JumpToStep,
    QuitNavigation,
    SkipStep,
    _RICH_AVAILABLE,
    get_console,
    confirm,
)
from .state import WizardState

logger = logging.getLogger("feral.cli.setup.state_machine")


StepFn = Callable[[WizardState], "Awaitable[None] | None"]


# Steps that handle their own framing (welcome panel, finish summary)
# get no auto step header — the indicator would only add noise.
_NO_INDICATOR_STEPS = frozenset({"welcome", "finish"})


_STEP_TITLES = {
    "llm_provider": "LLM Provider",
    "llm_model": "Model",
    "voice_preflight": "Voice providers",
    "audio": "Speech in / out",
    "identity": "Identity",
    "personality": "Personality",
    "capabilities": "Capabilities",
    "memory": "Memory & semantic search",
    "network": "Network access",
    "integrations": "Integrations",
    "home_assistant": "Home Assistant",
    "tool_keys": "Tool API keys",
    "external_agents": "External coding agents",
    "channels": "Messaging channels",
    "pairing": "Connect your phone",
    "tcc_preflight": "System permissions",
}


class StateMachine:
    """Run steps in order, supporting ``back`` and ``quit`` navigation."""

    def __init__(
        self,
        *,
        state: WizardState,
        steps: Sequence[tuple[str, StepFn]],
        from_step: str = "",
    ):
        self.state = state
        self.steps = list(steps)
        self.console = get_console()
        # Lane U1 — explicit re-entry point. When set, skip the
        # resume-sidecar prompt entirely and jump straight to the
        # named step so operators can re-run the LLM model picker
        # (or any other single step) without deleting
        # ``~/.feral/setup_state.json`` by hand.
        self.from_step = (from_step or "").strip()

    async def run(self) -> None:
        # Total visible steps for the "Step N of M" indicator excludes
        # the framing-only welcome/finish so the operator sees the
        # familiar 1..N progress count and not a meaningless 0..N+1.
        visible_steps = [s for s, _ in self.steps if s not in _NO_INDICATOR_STEPS]
        total_visible = len(visible_steps)
        visible_idx = 0

        # Lane U1 — explicit --from-step short-circuits the resume
        # prompt and jumps straight to the requested step. Useful for
        # re-running just the LLM model picker after a typo without
        # walking the whole wizard again.
        if self.from_step:
            try:
                idx = next(
                    i for i, (name, _) in enumerate(self.steps)
                    if name == self.from_step
                )
            except StopIteration:
                known = ", ".join(name for name, _ in self.steps)
                self.console.print(
                    f"[yellow]Unknown setup step {self.from_step!r}.[/] "
                    f"Known: {known}"
                    if _RICH_AVAILABLE
                    else f"Unknown setup step {self.from_step!r}. Known: {known}"
                )
                return
        else:
            # Lane 07  — resume support. If a sidecar exists from a
            # previous interrupted run, offer to skip ahead to the next
            # un-completed step instead of forcing the user to re-walk
            # the provider / model / audio prompts.
            idx = self._maybe_resume()

        while idx < len(self.steps):
            name, fn = self.steps[idx]
            if name not in _NO_INDICATOR_STEPS:
                visible_idx = visible_steps.index(name) + 1
                self._announce_step(name, visible_idx, total_visible)
            try:
                result = fn(self.state)
                if inspect.isawaitable(result):
                    await result
                self.state.completed_steps.add(name)
                # Persist resume sidecar after every successful step
                # so a Ctrl+C / crash on the NEXT step doesn't lose
                # the progress we just made. The finish step calls
                # ``state.mark_complete()`` which deletes the sidecar
                # — we MUST NOT re-write it here for the finish step,
                # otherwise the operator's next ``feral setup`` would
                # see a stale "resume?" prompt for an install that
                # already completed.
                self._persist_resume_marker(name)
                idx += 1
            except BackNavigation:
                if idx == 0:
                    self.console.print("(can't go back from the first step)")
                    continue
                idx -= 1
            except JumpToStep as jump:
                # Lane U2 follow-up — non-linear hop back to an earlier
                # step. The operator typed ``menu`` or picked the "↺
                # jump to a previous step…" affordance from any prompt;
                # honour the requested target (or show a picker when
                # no target was supplied) and set ``idx`` to that step.
                #
                # Already-entered settings / credentials are NEVER
                # wiped: ``self.state`` is the source of truth and the
                # operator is expected to re-walk the picked step to
                # overwrite a single answer (e.g. swap the OpenAI key
                # for a fresh one) before forward-navigating back to
                # where they were. Closes the operator complaint that
                # the wizard "doesn't allow going all the way back to
                # setup another provider key".
                target_idx = self._resolve_jump_target(jump.target, current_idx=idx)
                if target_idx is None:
                    # Operator cancelled the jump picker — stay on the
                    # current step and re-prompt instead of advancing.
                    continue
                idx = target_idx
            except SkipStep:
                # A skipped step is a DONE step: the operator answered
                # its question (with "no") or it had nothing to ask.
                # Leaving it out of ``completed_steps`` and out of the
                # resume sidecar meant a Ctrl+C on the next step
                # rewound to the step the operator had just explicitly
                # skipped, and the jump picker under-reported progress.
                self.state.completed_steps.add(name)
                self._persist_resume_marker(name)
                idx += 1
            except QuitNavigation:
                # Sidecar already written by the last successful
                # step; persist current settings/credentials so a
                # half-typed key isn't lost, then exit. We do NOT
                # mark_complete here — that's the finish step's job.
                try:
                    self.state.save()
                except Exception:
                    logger.debug("setup: state.save on quit failed", exc_info=True)
                self.console.print("Setup paused — run `feral setup` again to resume.")
                return
            except Exception as exc:
                # Unexpected error in a step: log + continue so the user
                # can still finish the wizard instead of the whole thing
                # exploding. The step's own error handling should catch
                # recoverable issues before this.
                logger.exception("step %s raised", name)
                self.console.print(f"[red]Step {name!r} failed: {exc}.[/] Continuing.")
                idx += 1

    def _persist_resume_marker(self, name: str) -> None:
        """Write the resume sidecar after a step finished or was skipped.

        Persisted after every step so a Ctrl+C / crash on the NEXT step
        doesn't lose the progress just made. The finish step calls
        ``state.mark_complete()``, which deletes the sidecar, so we must
        NOT re-write it for an install that already completed, otherwise
        the operator's next ``feral setup`` sees a stale "resume?"
        prompt.
        """
        already_complete = bool(
            (self.state.settings.get("meta") or {}).get("setup_complete")
        )
        if already_complete:
            return
        try:
            self.state.write_setup_state(
                last_step=name,
                completed_steps=sorted(self.state.completed_steps),
            )
        except Exception:
            logger.debug("setup: could not persist resume sidecar", exc_info=True)

    def _maybe_resume(self) -> int:
        """Read the resume sidecar (if any) and return the starting idx.

        Asks the operator whether to resume; if yes, returns the
        index AFTER the last completed step. If no, deletes the
        sidecar (operator chose a fresh start) and returns 0.
        """
        sidecar = WizardState.read_setup_state(self.state.home)
        last_step = sidecar.get("last_step", "")
        if not last_step:
            return 0

        try:
            resume_at = next(
                i for i, (name, _) in enumerate(self.steps)
                if name == last_step
            ) + 1
        except StopIteration:
            return 0
        if resume_at >= len(self.steps):
            return 0  # the sidecar references a step past the end — start over

        completed = sidecar.get("completed_steps") or []
        try:
            answer = confirm(
                f"Found a resume marker (last step: {last_step!r}, "
                f"{len(completed)} step(s) completed). "
                f"Skip ahead to the next un-completed step?",
                default=True,
            )
        except (KeyboardInterrupt, QuitNavigation, BackNavigation):
            answer = False

        if answer:
            # Restore the completed_steps set so the resume "looks like"
            # the same run — e.g. so finish step sees completed_steps
            # populated correctly.
            self.state.completed_steps |= set(completed)
            return resume_at

        # Operator chose to start over — drop the sidecar so it's not
        # offered again on subsequent runs.
        try:
            (self.state.home / "setup_state.json").unlink()
        except OSError:
            pass
        return 0

    def _resolve_jump_target(
        self, target: str, *, current_idx: int,
    ) -> "int | None":
        """Map a :class:`JumpToStep` target to a step index.

        When ``target`` is a known step name we return its index
        directly. When it's empty we render a picker over every
        visible step so the operator can pick one. Returns ``None``
        if the operator cancels the picker (we stay on the current
        step rather than silently advancing).
        """
        names = [name for name, _ in self.steps]
        if target:
            for i, name in enumerate(names):
                if name == target:
                    return i
            self.console.print(
                f"[yellow]Unknown step {target!r} — staying put.[/]"
                if _RICH_AVAILABLE else f"Unknown step {target!r} — staying put."
            )
            return None

        # Build a picker over every visible step. We highlight the
        # current step + completed steps so the operator can tell at
        # a glance what they've already filled in.
        from cli import ui_kit

        choices: list[dict] = []
        for i, name in enumerate(names):
            if name in _NO_INDICATOR_STEPS:
                continue
            title = _STEP_TITLES.get(name, name.replace("_", " ").title())
            badge = ""
            if name in self.state.completed_steps:
                badge = "  · completed"
            if i == current_idx:
                badge = "  · current"
            choices.append({
                "name": f"{title}{badge}",
                "value": name,
            })
        choices.append({"name": "(cancel — stay on current step)", "value": ""})

        if ui_kit.is_inquirer_available() and ui_kit.is_interactive():
            try:
                picked = ui_kit.pick(
                    "Jump to which step?",
                    choices,
                    default=names[current_idx] if current_idx < len(names) else None,
                )
            except KeyboardInterrupt:
                return None
        else:
            # Typed fallback — show a numbered menu.
            self.console.print("Jump to which step?")
            for i, c in enumerate(choices, start=1):
                self.console.print(f"  {i}. {c['name']}")
            raw = ""
            try:
                import sys as _sys
                _sys.stdout.write("  > ")
                _sys.stdout.flush()
                raw = (_sys.stdin.readline() or "").strip()
            except Exception:
                return None
            if not raw:
                return None
            if raw.isdigit():
                num = int(raw) - 1
                if 0 <= num < len(choices):
                    picked = choices[num]["value"]
                else:
                    return None
            else:
                picked = next(
                    (c["value"] for c in choices if c["name"].lower().startswith(raw.lower())),
                    "",
                )

        if not picked:
            return None
        for i, name in enumerate(names):
            if name == picked:
                return i
        return None

    def _announce_step(self, name: str, idx: int, total: int) -> None:
        title = _STEP_TITLES.get(name, name.replace("_", " ").title())
        if _RICH_AVAILABLE:
            self.console.print()
            self.console.print(
                f"[{ui_kit.BRAND_COLOR}]──[/] [bold]Step {idx} of {total}[/] "
                f"[dim]·[/] [bold]{title}[/] "
                f"[{ui_kit.BRAND_COLOR}]" + "─" * 4 + "[/]"
            )
        else:
            self.console.print()
            self.console.print(f"── Step {idx} of {total} · {title} ────")
