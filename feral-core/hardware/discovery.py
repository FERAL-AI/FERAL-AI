"""Brain-local hardware discovery (USB/host-attached devices).

Config-driven: each entry in :data:`DEVICE_DISCOVERY_SPECS` names a companion
library (module + class), a no-arg availability probe, and which adapter to
wrap it in. Adding a brand-new device class to the brain therefore needs only
its companion lib on the path plus one spec entry — no new discovery code.

The CuteBot keeps its bespoke :class:`CuteBotAdapter` (battery gate, drive
clamp, telemetry/episode loop). Any other self-describing device uses the
generic passthrough :class:`GenericSelfDescribingAdapter`.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("feral.hardware.discovery")

# cuteferalbot ships as a plain repo (no pyproject/setup.py). When it is not
# installed as a package, prepend the repo root so ``import cutebot`` works.
_CUTEBOT_PATH_CANDIDATES = (
    os.environ.get("FERAL_CUTEBOT_PATH"),
    str(Path.home() / "Desktop" / "cuteferalbot"),
    str(Path(__file__).resolve().parents[3] / "cuteferalbot"),
)


@dataclass
class DeviceDiscoverySpec:
    """Declarative description of one discoverable brain-local device class."""

    device_id: str
    module: str
    class_name: str
    # Static/class method on the device class returning bool "is present?".
    probe: str = "available"
    # "cutebot" -> bespoke CuteBotAdapter; "generic" -> passthrough adapter.
    adapter: str = "generic"
    # Marker file (relative to a candidate root) proving the lib lives there.
    path_marker: str = ""
    path_candidates: tuple[Optional[str], ...] = ()
    identity: dict[str, Any] = field(default_factory=dict)
    type_aliases: dict[str, str] = field(default_factory=dict)
    telemetry_key: Optional[str] = None


DEVICE_DISCOVERY_SPECS: list[DeviceDiscoverySpec] = [
    DeviceDiscoverySpec(
        device_id="cutebot-usb-0",
        module="cutebot.device",
        class_name="QtBot",
        probe="available",
        adapter="cutebot",
        path_marker="cutebot/device.py",
        path_candidates=_CUTEBOT_PATH_CANDIDATES,
    ),
]


def _ensure_on_path(spec: DeviceDiscoverySpec) -> None:
    if not spec.path_marker:
        return
    for candidate in spec.path_candidates:
        if not candidate:
            continue
        root = Path(candidate)
        if (root / spec.path_marker).is_file():
            path = str(root)
            if path not in sys.path:
                sys.path.insert(0, path)
            return


def _load_device_class(spec: DeviceDiscoverySpec) -> Optional[type]:
    try:
        module = importlib.import_module(spec.module)
    except ImportError:
        _ensure_on_path(spec)
        try:
            module = importlib.import_module(spec.module)
        except ImportError as exc:
            logger.debug("device lib %s unavailable: %s", spec.module, exc)
            return None
    return getattr(module, spec.class_name, None)


def _is_available(cls: type, spec: DeviceDiscoverySpec) -> bool:
    probe = getattr(cls, spec.probe, None)
    if probe is None:
        # No probe -> assume present (caller's spec opted out of probing).
        return True
    try:
        return bool(probe())
    except Exception as exc:
        logger.debug("%s availability probe failed: %s", spec.device_id, exc)
        return False


def _build_adapter(
    cls: type,
    spec: DeviceDiscoverySpec,
    *,
    memory: Any = None,
    emergency_stop_enabled: bool = True,
):
    if spec.adapter == "cutebot":
        from hardware.adapters.cutebot import CuteBotAdapter

        return CuteBotAdapter(
            device_id=spec.device_id,
            memory=memory,
            emergency_stop_enabled=emergency_stop_enabled,
        )

    from hardware.adapters.generic import GenericSelfDescribingAdapter

    return GenericSelfDescribingAdapter(
        spec.device_id,
        device_factory=cls,
        identity=spec.identity,
        type_aliases=spec.type_aliases,
        memory=memory,
        telemetry_key=spec.telemetry_key,
    )


def discover_brain_local_devices(
    *, memory: Any = None, emergency_stop_enabled: bool = True
) -> list[Any]:
    """Return constructed-but-not-connected adapters for present local devices."""
    adapters: list[Any] = []
    for spec in DEVICE_DISCOVERY_SPECS:
        cls = _load_device_class(spec)
        if cls is None:
            continue
        if not _is_available(cls, spec):
            continue
        try:
            adapters.append(
                _build_adapter(
                    cls, spec, memory=memory,
                    emergency_stop_enabled=emergency_stop_enabled,
                )
            )
        except Exception as exc:
            logger.warning("Failed to build adapter for %s: %s", spec.device_id, exc)
    return adapters
