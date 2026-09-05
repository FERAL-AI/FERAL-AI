"""
FERAL Continuous Screen Capture Pipeline
==========================================
Background async loop that periodically screenshots the desktop, sends the
image to a vision LLM for a one-sentence description, and feeds the result
into the perception frame + episodic memory.

Architecture:
  ScreenLoop  ─── captures & downscales ──▶  LLM vision API
       │                                          │
       │◀──── scene_description ──────────────────┘
       │
       ├──▶  PerceptionEngine.get_frame()   (updates vision fields)
       ├──▶  MemoryStore.episode_save()     (on significant transitions)
       └──▶  optional callback              (for UI / other subsystems)
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import platform
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agents.llm_provider import LLMProvider
    from cost.loop_guard import BudgetLoopGuard
    from memory.store import MemoryStore
    from perception.fusion import PerceptionEngine
    from perception.scene import SceneAnalyzer

logger = logging.getLogger("feral.perception.screen")

# Conservative token estimates for the cost guard's reservation.
# ``_VISION_PROMPT`` is ~25 tokens; the LLM reply is capped at 120.
# The reservation uses the reply ceiling because the input side is
# image-token-driven and modelled separately by the pricing module.
_VISION_RESERVATION_TOKENS = 120

_VISION_PROMPT = (
    "Describe in one sentence what the user is doing on their screen. "
    "Focus on the active application and the primary task visible."
)

_TRANSITION_KEYWORDS = {
    "error": ["error", "exception", "traceback", "failed", "crash", "fatal"],
    "app_switch": ["switched to", "opened", "launched", "now using", "moved to"],
    "document": ["editing", "document", "writing", "spreadsheet", "presentation"],
    "browsing": ["browser", "searching", "website", "tab", "navigating"],
    "terminal": ["terminal", "command line", "shell", "running command"],
    "ide": ["code", "editor", "IDE", "debugging", "programming"],
}

DOWNSCALE_WIDTH = 640

# How many consecutive description-less ticks before the loop shouts.
# At the default 8s interval this is ~2.5 minutes of capturing frames
# and learning nothing, which is well past "the VLM was briefly busy"
# and firmly into "vision is broken and nobody has been told".
_BLIND_TICK_ALERT_EVERY = 20


# ── Transition Detection ────────────────────────────────────────────

@dataclass
class TransitionEvent:
    """A notable change in what the user is doing on screen."""
    timestamp: float
    kind: str           # "app_switch", "error", "new_document", "idle_change", "general"
    previous: str
    current: str
    confidence: float   # 0.0–1.0


class ScreenTransitionDetector:
    """
    Compares consecutive scene descriptions and emits TransitionEvent
    objects when something notable changes.
    """

    def __init__(self, similarity_threshold: float = 0.55):
        self._similarity_threshold = similarity_threshold
        self._previous: str = ""

    def detect(self, new_description: str) -> Optional[TransitionEvent]:
        if not self._previous:
            self._previous = new_description
            return None

        sim = self._jaccard(self._previous, new_description)
        old = self._previous
        self._previous = new_description

        if sim >= self._similarity_threshold:
            return None

        kind = self._classify_transition(old, new_description)
        return TransitionEvent(
            timestamp=time.time(),
            kind=kind,
            previous=old,
            current=new_description,
            confidence=round(1.0 - sim, 2),
        )

    def reset(self):
        self._previous = ""

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        sa = set(a.lower().split())
        sb = set(b.lower().split())
        if not sa and not sb:
            return 1.0
        inter = sa & sb
        union = sa | sb
        return len(inter) / len(union) if union else 1.0

    @staticmethod
    def _classify_transition(old: str, new: str) -> str:
        combined = f"{old} {new}".lower()
        for kind, keywords in _TRANSITION_KEYWORDS.items():
            if any(kw in combined for kw in keywords):
                return kind
        return "general"


# ── Screen Capture Helpers ──────────────────────────────────────────

async def _capture_screenshot(save_path: Path) -> bool:
    """Platform-aware screenshot to *save_path*. Returns True on success."""
    system = platform.system().lower()
    if system == "darwin":
        cmd = ["screencapture", "-x", str(save_path)]
    elif system == "linux":
        import shutil
        if shutil.which("import"):
            cmd = ["import", "-window", "root", str(save_path)]
        elif shutil.which("gnome-screenshot"):
            cmd = ["gnome-screenshot", "-f", str(save_path)]
        elif shutil.which("scrot"):
            cmd = ["scrot", str(save_path)]
        else:
            logger.warning("No screenshot tool found on Linux")
            return False
    else:
        logger.warning(f"Screen capture unsupported on {system}")
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            logger.warning("Screenshot command failed: %s", stderr.decode(errors="replace").strip())
            return False
        return save_path.exists() and save_path.stat().st_size > 0
    except asyncio.TimeoutError:
        logger.warning("Screenshot capture timed out")
        return False
    except Exception as exc:
        logger.warning("Screenshot error: %s", exc)
        return False


def _downscale_and_encode(raw: bytes, target_width: int = DOWNSCALE_WIDTH) -> tuple[str, str]:
    """
    Downscale the image to *target_width* and return (base64_jpeg, mime).
    Falls back to raw base64 if Pillow is unavailable.
    """
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        if img.width > target_width:
            ratio = target_width / img.width
            img = img.resize((target_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except ImportError:
        logger.debug("Pillow not installed — sending full-size screenshot to LLM")
        return base64.b64encode(raw).decode("ascii"), "image/png"


async def _ask_vision_llm(llm: "LLMProvider", image_b64: str, mime: str) -> Optional[str]:
    """Send image to the LLM vision endpoint and return the description."""
    data_url = f"data:{mime};base64,{image_b64}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
            ],
        }
    ]
    try:
        resp = await llm.chat(messages, tools=None, temperature=0.3, max_tokens=120)
        text, _ = llm.extract_response(resp)
        return text.strip() if text else None
    except Exception as exc:
        logger.warning("Vision LLM call failed: %s", exc)
        return None


# ── Main Loop ───────────────────────────────────────────────────────

class ScreenLoop:
    """
    Continuously captures the screen, describes it via a vision LLM,
    and pipes the result into the perception + memory subsystems.

    Usage::

        loop = ScreenLoop(
            perception=perception_engine,
            memory=memory_store,
            llm=llm_provider,
            interval=8.0,
        )
        await loop.start()
        # ... later ...
        await loop.stop()
    """

    def __init__(
        self,
        *,
        perception: Optional["PerceptionEngine"] = None,
        memory: Optional["MemoryStore"] = None,
        llm: Optional["LLMProvider"] = None,
        scene_analyzer: Optional["SceneAnalyzer"] = None,
        interval: float = 8.0,
        session_id: str = "screen_loop",
        on_transition: Optional[Callable[[TransitionEvent], Any]] = None,
        cost_guard: Optional["BudgetLoopGuard"] = None,
        model: str = "gpt-4o-mini",
    ):
        self._perception = perception
        self._memory = memory
        self._llm = llm
        self._scene_analyzer = scene_analyzer
        self._interval = max(1.0, interval)
        self._session_id = session_id
        self._on_transition = on_transition
        # audit-r14 / S6 — gate every paid vision LLM call through the
        # shared loop guard. When the guard is None the loop runs
        # unbounded (operator opted out / cost tracking disabled).
        self._cost_guard = cost_guard
        self._cost_model = model
        self._budget_pause_count = 0

        self._detector = ScreenTransitionDetector()
        self._task: Optional[asyncio.Task] = None
        self._running = False

        self._capture_count = 0
        self._error_count = 0
        self._blind_ticks = 0
        self._vision_unreachable_skips = 0
        self._last_description = ""
        self._tmp_path = Path(f"/tmp/feral_screen_loop_{id(self)}.png")

    # ── public API ──────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def stats(self) -> dict:
        return {
            "running": self.is_running,
            "interval": self._interval,
            "captures": self._capture_count,
            "errors": self._error_count,
            # Non-zero means the loop is capturing but seeing nothing.
            # ``captures`` alone reads as healthy in that state, which is
            # how the vision outage stayed invisible.
            "blind_ticks": self._blind_ticks,
            "last_description": self._last_description,
            "budget_pauses": self._budget_pause_count,
            "budget_paused": bool(
                self._cost_guard and self._cost_guard.is_paused
            ),
            # Ticks skipped because the vision provider is unreachable
            # and inside its backoff. Distinct from ``blind_ticks``,
            # which means "we called and got nothing back": this means
            # "we did not call, and here is why".
            "vision_unreachable_skips": self._vision_unreachable_skips,
            "vision_provider": (
                self._scene_analyzer.provider_health
                if self._scene_analyzer is not None
                and hasattr(self._scene_analyzer, "provider_health")
                else {}
            ),
        }

    async def start(self):
        if self.is_running:
            logger.info("Screen loop already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="feral-screen-loop")
        logger.info("Screen loop started (interval=%.1fs)", self._interval)

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._detector.reset()
        self._cleanup_tmp()
        logger.info("Screen loop stopped")

    # ── internals ───────────────────────────────────────────────────

    async def _loop(self):
        logger.debug("Screen loop coroutine entered")
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._error_count += 1
                logger.error("Screen loop tick failed: %s", exc, exc_info=True)
            await asyncio.sleep(self._interval)

    async def _tick(self):
        # audit-r14 / S6 — pre-flight against the cost cap BEFORE
        # screenshotting. ``allow()`` returns False once the projected
        # vision spend would exceed the hourly cap; the guard then
        # auto-pauses the loop for the rest of the cap window and
        # emits a ``cost_cap_hit`` WS frame for the UI banner.
        if self._cost_guard is not None and not self._cost_guard.allow(
            model=self._cost_model,
            estimated_max_tokens=_VISION_RESERVATION_TOKENS,
        ):
            self._budget_pause_count += 1
            return

        ok = await _capture_screenshot(self._tmp_path)
        if not ok:
            self._error_count += 1
            return

        raw = self._tmp_path.read_bytes()
        image_b64, mime = _downscale_and_encode(raw)
        self._capture_count += 1

        description: Optional[str] = None
        detected: Optional[list[str]] = None

        # ``configured`` rather than ``available``: a SceneAnalyzer whose
        # provider is inside its unreachable backoff window is still THE
        # vision path for this install. Branching on ``available`` here
        # would silently fall through to the shared LLM below and start
        # billing the operator's chat provider for screen frames the
        # moment their local Ollama went down.
        scene = self._scene_analyzer
        if scene is not None and getattr(scene, "configured", scene.available):
            if scene.available:
                encoding = "jpeg" if mime == "image/jpeg" else "png"
                # A6 — periodic ticks MUST respect the SceneAnalyzer
                # cooldown. The previous ``force=True`` bypassed the
                # cooldown on every tick, so a 10s cooldown + 8s tick
                # interval still issued a vision-model call every 8s.
                # The cooldown is operator-tunable via
                # ``FERAL_SCENE_COOLDOWN`` / ``vision.scene_cooldown``;
                # letting it apply here is the whole point of that knob.
                result = await scene.analyze_frame(
                    image_b64, encoding=encoding, node_id=self._session_id,
                )
                if result:
                    description = result.get("scene_description")
                    detected = result.get("detected_objects")
            else:
                # Unreachable and backing off. SceneAnalyzer has already
                # said so once, with a next-probe time; saying it again
                # every 8s is the noise this replaces.
                self._vision_unreachable_skips += 1
        elif self._llm and self._llm.available:
            description = await _ask_vision_llm(self._llm, image_b64, mime)

        # audit-r14 / S6 — record the post-call usage so the rollup
        # reflects the real spend. We don't have an exact prompt/
        # completion token count from the vision endpoint in the
        # common path; pass the reservation as an upper bound on the
        # completion side and 0 on the prompt side (image tokens are
        # priced separately in the catalog).
        # ...and only when there was a call to bill. A tick that produced
        # no description either never reached the provider (unreachable,
        # backing off) or got nothing back, and in neither case did the
        # operator spend the 120-token vision reservation. Recording it
        # anyway is what wrote 413 zero-token cost rows in a single day,
        # every one of them attributing spend to a call that never
        # happened, and it is what would have tripped the hourly cap and
        # paused a loop that was not costing anything.
        if self._cost_guard is not None and description:
            # Bookkeeping MUST NOT be able to destroy an observation we
            # already paid the VLM for. This await used to run bare, and
            # a CostBudget whose asyncio.Lock was bound to another event
            # loop raised RuntimeError here on every single tick: 1,548
            # ticks between 2026-07-07 and 2026-07-26 captured a frame,
            # called the VLM, and then threw the description away before
            # reaching the episode save below.
            try:
                await self._cost_guard.record(
                    model=self._cost_model,
                    prompt_tokens=0,
                    completion_tokens=_VISION_RESERVATION_TOKENS,
                )
            except Exception as exc:
                logger.warning("Screen loop cost accounting failed: %s", exc)

        if not description:
            # A screenshot that produces no description is a blind tick.
            # Silently returning here is how the pipeline went quiet for
            # a week while ``stats["captures"]`` kept climbing and the
            # loop reported healthy, so count the run of blind ticks and
            # escalate to WARNING once it is long enough to mean the VLM
            # is broken rather than momentarily unlucky.
            self._blind_ticks += 1
            if self._blind_ticks % _BLIND_TICK_ALERT_EVERY == 0:
                logger.warning(
                    "Screen loop has captured %d consecutive frames with no "
                    "scene description (%.0fs of blind capture). The vision "
                    "model is producing nothing usable; no screen episodes "
                    "are being recorded",
                    self._blind_ticks, self._blind_ticks * self._interval,
                )
            self._update_perception_frame(image_b64, mime)
            return

        self._blind_ticks = 0
        self._last_description = description
        if detected is None:
            detected = self._extract_objects(description)
        self._update_perception_frame(image_b64, mime, description, detected)

        transition = self._detector.detect(description)
        if transition:
            logger.info(
                "Screen transition [%s]: %s → %s (conf=%.2f)",
                transition.kind, transition.previous[:60], transition.current[:60],
                transition.confidence,
            )
            await self._record_transition(transition)

            if self._on_transition:
                try:
                    result = self._on_transition(transition)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.warning("Transition callback error: %s", exc)

    def _update_perception_frame(
        self,
        image_b64: str,
        mime: str,
        description: str = "",
        detected_objects: Optional[list[str]] = None,
    ):
        if not self._perception:
            return
        frame = self._perception.get_frame(self._session_id)
        frame.has_vision = True
        frame.vision_data_url = f"data:{mime};base64,{image_b64}"
        frame.timestamp = time.time()
        if description:
            frame.scene_description = description
        if detected_objects:
            frame.detected_objects = detected_objects

    async def _record_transition(self, t: TransitionEvent):
        if not self._memory:
            return
        await self._memory.episode_save(
            session_id=self._session_id,
            event_type=f"screen_{t.kind}",
            summary=f"Screen: {t.current}",
            detail=f"Previous: {t.previous}",
            importance=min(0.4 + t.confidence * 0.4, 0.9),
        )

    @staticmethod
    def _extract_objects(description: str) -> list[str]:
        """Heuristic extraction of likely app/object names from a sentence."""
        markers = [
            "Safari", "Chrome", "Firefox", "Terminal", "iTerm", "VS Code",
            "Xcode", "Slack", "Discord", "Finder", "Mail", "Calendar",
            "Notes", "Preview", "Pages", "Numbers", "Keynote", "Figma",
            "Spotify", "YouTube", "Excel", "Word", "PowerPoint", "Cursor",
        ]
        found = [m for m in markers if m.lower() in description.lower()]
        return found or []

    def _cleanup_tmp(self):
        try:
            self._tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
