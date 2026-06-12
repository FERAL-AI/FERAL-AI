"""Brain-local hardware discovery (USB-attached devices on the brain host)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hardware.adapters.cutebot import CuteBotAdapter

logger = logging.getLogger("feral.hardware.discovery")

# cuteferalbot ships as a plain repo (no pyproject/setup.py). When it is not
# installed as a package, prepend the repo root so ``import cutebot`` works.
_CUTEBOT_PATH_CANDIDATES = (
    os.environ.get("FERAL_CUTEBOT_PATH"),
    str(Path.home() / "Desktop" / "cuteferalbot"),
    str(Path(__file__).resolve().parents[3] / "cuteferalbot"),
)


def _ensure_cuteferalbot_path() -> bool:
    for candidate in _CUTEBOT_PATH_CANDIDATES:
        if not candidate:
            continue
        root = Path(candidate)
        if (root / "cutebot" / "device.py").is_file():
            path = str(root)
            if path not in sys.path:
                sys.path.insert(0, path)
            return True
    return False


def _import_qtbot():
    try:
        from cutebot.device import QtBot

        return QtBot
    except ImportError:
        if not _ensure_cuteferalbot_path():
            return None
        try:
            from cutebot.device import QtBot

            return QtBot
        except ImportError as exc:
            logger.debug("cuteferalbot unavailable: %s", exc)
            return None


def discover_brain_local_devices() -> list["CuteBotAdapter"]:
    """Return constructed-but-not-connected adapters for local USB devices."""
    QtBot = _import_qtbot()
    if QtBot is None:
        return []

    try:
        if not QtBot.available():
            return []
    except Exception as exc:
        logger.debug("CuteBot discovery check failed: %s", exc)
        return []

    from hardware.adapters.cutebot import CuteBotAdapter

    return [CuteBotAdapter()]
