"""Clicks must land where the model aimed, on any display.

The screenshot pipeline has three coordinate spaces and the click path
was using the wrong ratio between two of them:

  1. **native capture** - `screencapture` writes physical pixels, so a
     Retina display produces 3360 wide for a 1680-point screen.
  2. **what the model sees** - `encode_for_vlm` resizes anything wider
     than `_SCREENSHOT_MAX_WIDTH` (1920) and discards the ratio.
  3. **where pyautogui clicks** - logical points, 1680 wide.

`scale_coordinates` divided by the display's backing scale factor
(2.0), but the model's coordinates are in space 2, and the trip from
space 2 to space 3 is `1920 / 1680 = 1.143`. Every click landed at
roughly 57% of the intended position, an error that grows toward the
screen edge: a target at x=1900 was clicked at 950 instead of 1662.

That makes `agentic_computer_use`, the screenshot-understand-act loop,
unable to reliably click anything on a Retina Mac, while its manifest
claims it "supports Retina/HiDPI scaling automatically".

Both halves were already unit-tested in isolation and both were
individually correct. Nothing tested the composition, which is the only
place the bug exists. These tests are that composition.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.impl.gui_computer_use import (  # noqa: E402
    _SCREENSHOT_MAX_WIDTH,
    vlm_to_screen_divisor,
)


def _round_trip(logical_w: int, backing: float, model_x: int) -> int:
    """Where a click actually lands for a model coordinate."""
    divisor = vlm_to_screen_divisor(logical_w, backing)
    return int(model_x / divisor)


def _expected(logical_w: int, backing: float, model_x: int) -> int:
    """Where it should land, derived from the pipeline itself."""
    native_w = int(logical_w * backing)
    seen_by_model = min(native_w, _SCREENSHOT_MAX_WIDTH)
    return int(model_x / (seen_by_model / logical_w))


# ── the display this was found on ──────────────────────────────────

def test_retina_click_lands_where_the_model_aimed():
    """1680pt @2x: native 3360, model sees 1920, clicks in 1680."""
    for model_x in (100, 480, 960, 1440, 1900):
        got = _round_trip(1680, 2.0, model_x)
        want = _expected(1680, 2.0, model_x)
        assert got == want, (
            f"model aimed at x={model_x}, click landed at {got}, should be "
            f"{want} (off by {got - want}px)"
        )


def test_the_error_grows_toward_the_screen_edge():
    """The old bug was worst exactly where menus and buttons live."""
    divisor = vlm_to_screen_divisor(1680, 2.0)
    assert divisor == pytest.approx(1920 / 1680, rel=1e-6), (
        f"divisor {divisor} is not the ratio between the image the model "
        "sees and the space pyautogui clicks in"
    )
    assert _round_trip(1680, 2.0, 1900) == pytest.approx(1662, abs=2)


# ── every other display shape ──────────────────────────────────────

@pytest.mark.parametrize("logical_w,backing,label", [
    (1920, 1.0, "non-Retina 1080p: no resize, no scaling"),
    (1680, 2.0, "Retina 13in: native 3360 resized to 1920"),
    (2560, 2.0, "Retina 5K: model image is SMALLER than click space"),
    (1440, 2.0, "Retina 14in"),
    (3840, 1.0, "4K at 1x: native 3840 resized to 1920"),
])
def test_round_trip_is_correct_on_every_display_shape(logical_w, backing, label):
    for model_x in (0, 50, logical_w // 3, logical_w // 2):
        got = _round_trip(logical_w, backing, model_x)
        want = _expected(logical_w, backing, model_x)
        assert got == want, f"{label}: model x={model_x} -> {got}, want {want}"


def test_a_large_retina_display_scales_coordinates_up_not_down():
    """5K: 5120 native resized to 1920, but pyautogui space is 2560.

    The image the model sees is SMALLER than the click space, so the
    divisor is below 1 and coordinates scale UP. The old code, dividing
    by the backing factor, could only ever scale down and so could
    never produce this.
    """
    divisor = vlm_to_screen_divisor(2560, 2.0)
    assert divisor < 1.0, f"expected an upscale, got divisor {divisor}"
    assert _round_trip(2560, 2.0, 960) > 960


def test_a_screen_narrower_than_the_cap_is_untouched():
    """No resize happened, so no correction is owed."""
    assert vlm_to_screen_divisor(1440, 1.0) == pytest.approx(1.0)
    assert _round_trip(1440, 1.0, 700) == 700


# ── guards ─────────────────────────────────────────────────────────

def test_divisor_is_never_zero_or_negative():
    """A bad probe must not produce a division by zero on the click path."""
    for logical_w, backing in ((0, 2.0), (-1, 2.0), (1680, 0.0), (1680, -2.0)):
        assert vlm_to_screen_divisor(logical_w, backing) > 0


def test_the_click_path_uses_the_pipeline_divisor():
    """Pin the wiring: the fix is worthless if the caller still divides
    by the raw backing scale factor."""
    src = (ROOT / "skills" / "impl" / "gui_computer_use.py").read_text()
    assert "vlm_to_screen_divisor" in src, (
        "gui_computer_use does not derive its divisor from the screenshot "
        "pipeline; clicks will keep using the display scale factor"
    )
