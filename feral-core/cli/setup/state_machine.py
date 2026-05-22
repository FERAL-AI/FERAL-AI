"""Tiny state machine that drives the linear wizard flow with back/quit.

audit-r14 / lane-07 W7 — adds resume support. After every successful
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

from .helpers import BackNavigation, QuitNavigation, SkipStep, _RICH_AVAILABLE, get_console, confirm
from .state import WizardState

logger = logging.getLogger("feral.cli.setup.state_machine")


StepFn = Callable[[WizardState], "Awaitable[None] | None"]


# Steps that handle their own framing (welcome panel, finish summary)
# get no auto step header — the indicator would only add noise.
_NO_INDICATOR_STEPS = frozenset({"welcome", "finish"})


_STEP_TITLES = {
    "llm_provider": "LLM Provider",
    "llm_model": "Model",
    "audio": "Speech in / out",
    "identity": "Identity",
    "network": "Network access",
    "home_assistant": "Home Assistant",
    "channels": "Messaging channels",
}


class StateMachine:
    """Run steps in order, supporting ``back`` and ``quit`` navigation."""

    def __init__(self, *, state: WizardState, steps: Sequence[tuple[str, StepFn]]):
        self.state = state
        self.steps = list(steps)
        self.console = get_console()

    async def run(self) -> None:
        # Total visible steps for the "Step N of M" indicator excludes
        # the framing-only welcome/finish so the operator sees the
        # familiar 1..N progress count and not a meaningless 0..N+1.
        visible_steps = [s for s, _ in self.steps if s not in _NO_INDICATOR_STEPS]
        total_visible = len(visible_steps)
        visible_idx = 0

        # Lane 07 W7 — resume support. If a sidecar exists from a
        # previous interrupted run, offer to skip ahead to the next
        # un-completed step instead of forcing the user to re-walk the
        # provider / model / audio prompts.
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
                already_complete = bool(
                    (self.state.settings.get("meta") or {}).get("setup_complete")
                )
                if not already_complete:
                    try:
                        self.state.write_setup_state(
                            last_step=name,
                            completed_steps=sorted(self.state.completed_steps),
                        )
                    except Exception:
                        logger.debug(
                            "setup: could not persist resume sidecar",
                            exc_info=True,
                        )
                idx += 1
            except BackNavigation:
                if idx == 0:
                    self.console.print("(can't go back from the first step)")
                    continue
                idx -= 1
            except SkipStep:
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
