"""A dead vision provider is reported once, not every eight seconds.

From the 2026-09-04 audit: ``vision.provider=ollama`` with no Ollama
running produced 78 ERROR plus 78 WARNING lines from ``feral.scene`` in a
single morning, still climbing after a restart that night, all of them
saying "Ollama VLM call failed: All connection attempts failed". ScreenLoop
ticks every 8s and the scene cooldown is 10s, so that is a call every ~16s
forever. The failed calls still recorded the 120-token vision cost
reservation, which is where the day's 413 zero-token cost rows came from.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from perception.fusion import PerceptionEngine, PerceptionFrame  # noqa: E402
from perception.scene import (  # noqa: E402
    UNREACHABLE_AFTER_FAILURES,
    UNREACHABLE_BACKOFF_MAX_S,
    UNREACHABLE_BACKOFF_START_S,
    SceneAnalyzer,
    is_connection_error,
)
from perception.screen_loop import ScreenLoop  # noqa: E402

pytestmark = pytest.mark.asyncio


class _ConnectError(Exception):
    """Stands in for ``httpx.ConnectError`` without importing httpx."""


def _unreachable_analyzer(monkeypatch):
    monkeypatch.delenv("FERAL_VLM_PROVIDER", raising=False)
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(
        side_effect=_ConnectError("All connection attempts failed"),
    )
    llm.extract_response = MagicMock(return_value=("", []))
    analyzer = SceneAnalyzer(llm=llm)
    analyzer._cooldown = 0.0
    return analyzer


# ── Classifying the failure ──────────────────────────────────────────


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_ConnectError("All connection attempts failed"), True),
        (ConnectionRefusedError(61, "Connection refused"), True),
        (OSError(8, "nodename nor servname provided"), True),
        (TimeoutError("timed out"), True),
        (ValueError("model 'moondream' not found"), False),
        (RuntimeError("401 Unauthorized"), False),
    ],
)
async def test_connection_errors_are_distinguished_from_provider_errors(exc, expected):
    """A 401 from a live provider is a configuration problem the operator
    must see every time. A refused TCP connection is a fact that stays
    true until something changes."""
    assert is_connection_error(exc) is expected


# ── Marking the provider unreachable ─────────────────────────────────


async def test_three_connection_failures_mark_the_provider_unreachable(
    monkeypatch, caplog,
):
    analyzer = _unreachable_analyzer(monkeypatch)

    with caplog.at_level("WARNING", logger="feral.scene"):
        for _ in range(UNREACHABLE_AFTER_FAILURES):
            assert await analyzer.analyze_frame("AAAA==", force=True) is None

    assert analyzer.unreachable is True
    assert analyzer.available is False
    assert analyzer.configured is True, (
        "a provider that is configured but down is not the same as no "
        "provider at all, and the operator needs to tell them apart"
    )
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, (
        f"one warning per backoff step, got {[r.message for r in warnings]}"
    )
    assert "unreachable" in warnings[0].message


async def test_a_single_failure_does_not_take_vision_away(monkeypatch):
    analyzer = _unreachable_analyzer(monkeypatch)
    await analyzer.analyze_frame("AAAA==", force=True)
    assert analyzer.unreachable is False
    assert analyzer.available is True


async def test_no_error_level_logging_while_unreachable(monkeypatch, caplog):
    """78 ERROR lines in one morning, every one of them the same fact."""
    analyzer = _unreachable_analyzer(monkeypatch)

    with caplog.at_level("DEBUG", logger="feral.scene"):
        for _ in range(20):
            await analyzer.analyze_frame("AAAA==", force=True)

    assert [r for r in caplog.records if r.levelname == "ERROR"] == []


async def test_backoff_doubles_and_is_capped(monkeypatch):
    analyzer = _unreachable_analyzer(monkeypatch)
    for _ in range(UNREACHABLE_AFTER_FAILURES):
        await analyzer.analyze_frame("AAAA==", force=True)
    assert analyzer._backoff_s == UNREACHABLE_BACKOFF_START_S

    for _ in range(40):
        analyzer._note_vlm_connection_failure(_ConnectError("nope"))

    assert analyzer._backoff_s == UNREACHABLE_BACKOFF_MAX_S


async def test_the_provider_is_probed_again_once_the_window_expires(monkeypatch):
    analyzer = _unreachable_analyzer(monkeypatch)
    for _ in range(UNREACHABLE_AFTER_FAILURES):
        await analyzer.analyze_frame("AAAA==", force=True)
    assert analyzer.available is False

    analyzer._next_probe_at = 0.0  # window elapsed
    assert analyzer.available is True

    analyzer._llm.chat = AsyncMock(return_value={"choices": []})
    analyzer._llm.extract_response = MagicMock(
        return_value=('{"scene_description": "a desk"}', []),
    )
    result = await analyzer.analyze_frame("AAAA==", force=True)

    assert result == {"scene_description": "a desk"}
    assert analyzer.unreachable is False
    assert analyzer._connect_failures == 0


async def test_an_explicit_forced_call_still_probes(monkeypatch):
    """``force=True`` is only ever set by an explicit request (a screen
    capture, a "what do you see"), and an explicit request is exactly the
    probe the backoff is waiting for. The periodic loop does not force, so
    it keeps backing off."""
    analyzer = _unreachable_analyzer(monkeypatch)
    for _ in range(UNREACHABLE_AFTER_FAILURES):
        await analyzer.analyze_frame("AAAA==", force=True)
    assert analyzer.unreachable is True

    analyzer._llm.chat = AsyncMock(return_value={"choices": []})
    analyzer._llm.extract_response = MagicMock(
        return_value=('{"scene_description": "a desk"}', []),
    )

    assert await analyzer.analyze_frame("AAAA==", force=False) is None
    assert await analyzer.analyze_frame("AAAA==", force=True) == {
        "scene_description": "a desk",
    }
    assert analyzer.unreachable is False


async def test_provider_health_says_what_is_wrong(monkeypatch):
    analyzer = _unreachable_analyzer(monkeypatch)
    for _ in range(UNREACHABLE_AFTER_FAILURES):
        await analyzer.analyze_frame("AAAA==", force=True)

    health = analyzer.provider_health

    assert health["unreachable"] is True
    assert health["configured"] is True
    assert health["available"] is False
    assert health["consecutive_connection_failures"] == UNREACHABLE_AFTER_FAILURES
    assert "All connection attempts failed" in health["detail"]
    assert health["next_probe_in_s"] > 0


# ── The loop stops paying for it ─────────────────────────────────────


def _screen_loop(scene, cost_guard=None):
    perception = MagicMock(spec=PerceptionEngine)
    perception.get_frame.return_value = PerceptionFrame()
    loop = ScreenLoop(
        perception=perception, scene_analyzer=scene, interval=8.0,
        cost_guard=cost_guard,
    )
    loop._tmp_path.write_bytes(b"\xff\xd8\xff\xe0" + bytes(300))
    return loop


async def _tick(loop, times=1):
    with patch(
        "perception.screen_loop._capture_screenshot",
        new_callable=AsyncMock, return_value=True,
    ), patch(
        "perception.screen_loop._downscale_and_encode",
        return_value=("bbb", "image/jpeg"),
    ):
        for _ in range(times):
            await loop._tick()
    try:
        loop._tmp_path.unlink(missing_ok=True)
    except OSError:
        pass


async def test_unreachable_provider_is_not_called_and_is_not_billed():
    """413 zero-token cost rows in one day, every one of them attributing
    spend to a call that never produced anything."""
    scene = MagicMock()
    scene.configured = True
    scene.available = False
    scene.provider_health = {"unreachable": True}
    scene.analyze_frame = AsyncMock(return_value=None)

    guard = MagicMock()
    guard.allow.return_value = True
    guard.is_paused = False
    guard.record = AsyncMock()

    loop = _screen_loop(scene, cost_guard=guard)
    await _tick(loop, times=5)

    scene.analyze_frame.assert_not_awaited()
    guard.record.assert_not_awaited()
    assert loop.stats["vision_unreachable_skips"] == 5
    assert loop.stats["vision_provider"] == {"unreachable": True}


async def test_a_blind_tick_is_not_billed_either():
    """The provider answered, with nothing. No description, no spend."""
    scene = MagicMock()
    scene.configured = True
    scene.available = True
    scene.provider_health = {}
    scene.analyze_frame = AsyncMock(return_value=None)

    guard = MagicMock()
    guard.allow.return_value = True
    guard.is_paused = False
    guard.record = AsyncMock()

    loop = _screen_loop(scene, cost_guard=guard)
    await _tick(loop, times=3)

    guard.record.assert_not_awaited()
    assert loop.stats["blind_ticks"] == 3


async def test_a_real_description_is_still_billed():
    scene = MagicMock()
    scene.configured = True
    scene.available = True
    scene.provider_health = {}
    scene.analyze_frame = AsyncMock(
        return_value={"scene_description": "Reading a PR diff"},
    )

    guard = MagicMock()
    guard.allow.return_value = True
    guard.is_paused = False
    guard.record = AsyncMock()

    loop = _screen_loop(scene, cost_guard=guard)
    await _tick(loop)

    guard.record.assert_awaited_once()


async def test_an_unreachable_local_vlm_does_not_fall_through_to_the_paid_llm():
    """Branching on ``available`` here would silently start billing the
    operator's chat provider for screen frames the moment their local
    Ollama went down."""
    scene = MagicMock()
    scene.configured = True
    scene.available = False
    scene.provider_health = {"unreachable": True}
    scene.analyze_frame = AsyncMock(return_value=None)

    llm = MagicMock()
    llm.available = True

    perception = MagicMock(spec=PerceptionEngine)
    perception.get_frame.return_value = PerceptionFrame()
    loop = ScreenLoop(
        perception=perception, scene_analyzer=scene, llm=llm, interval=8.0,
    )
    loop._tmp_path.write_bytes(b"\xff\xd8\xff\xe0" + bytes(300))

    with patch(
        "perception.screen_loop._ask_vision_llm", new_callable=AsyncMock,
    ) as ask:
        await _tick(loop, times=3)

    ask.assert_not_awaited()
