"""
FERAL Perception Fusion Engine
=================================
The core differentiator: fuses camera, audio, and sensor data into a
single unified PerceptionFrame that becomes the LLM's ground truth.

From Vision.md:
  "The brain's input is NOT text. It's a fused multimodal stream."

  {
    "timestamp": 1743000000,
    "audio": <transcript>,
    "vision": { scene_description, detected_objects, text_in_scene },
    "sensors": { heart_rate, activity, location, ambient_light },
    "gesture": null
  }
"""

from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from api.server import VisionBuffer

logger = logging.getLogger("feral.perception")


# Same window proactive engine uses; documented in
# `PerceptionFrame.to_system_context` docstring. Vital readings older
# than this drop out of the LLM context (or get tagged `(stale, …)`)
# so the model never quotes a stale Apple HealthKit value as live.
_CONTEXT_FRESH_S = 120.0


# Live-wearable sources are preferred over Apple HealthKit when both
# are emitting samples. Operator report 2026-06-05 demo prep: the
# iOS Context tab was reading HR=115 from ``apple_healthkit`` even
# though the W300 glasses + Veepoo wristband were also streaming.
# HealthKit's pipeline returns "resting HR" / "last measured" reads
# that can be hours stale and overwrites the live wearable value
# whenever its push lands second. Anything classified as a live
# wearable here wins the "current HR" slot whenever its sample is
# fresh, even if the HealthKit emit arrived microseconds later.
_LIVE_WEARABLE_SOURCES: frozenset[str] = frozenset({
    "jw_health_glasses",   # Theora W300 glasses (BLE PPG)
    "veepoo_wristband",    # Veepoo wristband (BLE PPG)
    "theora_w300",         # Legacy alias
    "w610_glasses",        # Future Theora W610
    "polar",               # Generic BLE chest strap
    "wahoo",               # Generic BLE chest strap
    "garmin_ble",          # Direct BLE Garmin
})

# Sources we know are NOT real-time. Even if the sample_ts says
# "0 seconds ago" (= the bridge resampled it now), the underlying
# reading might be hours old. Treated as "second-class" current —
# wins only when no live wearable is reporting, and gets tagged
# stale aggressively in ``to_system_context``.
_LAGGING_SOURCES: frozenset[str] = frozenset({
    "apple_healthkit",
    "healthkit",
    "google_fit",
    "fitbit_cloud",
    "whoop_cloud",
    "oura_cloud",
})


def _is_live_wearable(source: str) -> bool:
    return (source or "").strip().lower() in _LIVE_WEARABLE_SOURCES


def _is_lagging_source(source: str) -> bool:
    return (source or "").strip().lower() in _LAGGING_SOURCES


@dataclass
class PerceptionFrame:
    """
    A single fused perception snapshot.
    Every LLM invocation receives the latest frame as context.
    """
    timestamp: float = field(default_factory=time.time)

    # Audio
    transcript: str = ""
    audio_ambient: str = ""  # "silence", "speech", "music", "traffic", etc.

    # Vision
    has_vision: bool = False
    scene_description: str = ""
    detected_objects: list[str] = field(default_factory=list)
    text_in_scene: list[str] = field(default_factory=list)
    vision_data_url: str = ""  # base64 data URL of the latest frame

    # Sensors
    heart_rate: int = 0
    spo2_pct: int = 0
    skin_temperature_c: float = 0.0
    activity_state: str = "unknown"  # resting, walking, running, stressed

    # Per-metric sample times (operator report 2026-05-09: fake-looking
    # HR=115 alert when glasses weren't connected — actual cause was
    # Apple HealthKit returning a hours-stale resting-HR sample as
    # "current". Proactive alerts now read these timestamps to gate on
    # freshness AND surface the source so the user knows where it came
    # from). Default 0.0 means "never seen". Updated by
    # ``update_sensors`` whenever the matching metric receives a fresh
    # value with a known timestamp.
    heart_rate_sample_ts: float = 0.0
    heart_rate_source: str = ""  # e.g. "apple_healthkit", "theora_w300"
    spo2_sample_ts: float = 0.0
    spo2_source: str = ""
    head_pose: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    ambient_light_lux: int = 0
    battery_pct: int = 100
    location: Optional[dict] = None  # {"lat": float, "lon": float}

    # Gesture
    gesture: Optional[str] = None  # "tap", "swipe_left", "nod", etc.

    # Device
    connected_nodes: list[str] = field(default_factory=list)

    def to_system_context(self) -> str:
        """
        Serialize into a compact LLM-injectable context block.
        Only includes non-empty fields to minimize token usage.

        Freshness contract (operator report 2026-05-09 round 3): when
        the user asked "What's my heart rate?" the assistant generated
        a markdown table claiming "HR: 115 bpm — Elevated, SpO2: 93% —
        Normal". The numbers were real Apple HealthKit reads from
        hours earlier; the LLM treated them as current because this
        method injected ``Sensors: HR=115bpm | SpO2=93%`` into every
        prompt without saying when the sample was taken. Now:

          * Vitals readings within ``_CONTEXT_FRESH_S`` (default 120s
            of the frame's `*_sample_ts`) appear plain.
          * Older readings appear with `(stale, Xs ago)` suffix so the
            model knows not to quote them as live.
          * Readings with `*_sample_ts == 0.0` (sender never plumbed
            freshness) are SUPPRESSED entirely — same defensive
            default as the proactive freshness gate.

        Pinned by ``tests/test_perception_context_freshness.py``.
        """
        sections = []
        now = time.time()

        sensor_parts = []
        # Heart rate — gate on freshness to prevent the LLM from
        # treating a stale Apple HealthKit reading as real-time.
        if self.heart_rate:
            hr_ts = float(getattr(self, "heart_rate_sample_ts", 0.0) or 0.0)
            if hr_ts > 0:
                hr_age = now - hr_ts
                if hr_age <= _CONTEXT_FRESH_S:
                    sensor_parts.append(f"HR={self.heart_rate}bpm")
                else:
                    sensor_parts.append(
                        f"HR={self.heart_rate}bpm (stale, {int(hr_age)}s ago — do NOT report as current)"
                    )
            # else: sample_ts unset → suppress to avoid hallucination.
        if self.spo2_pct:
            spo2_ts = float(getattr(self, "spo2_sample_ts", 0.0) or 0.0)
            if spo2_ts > 0:
                spo2_age = now - spo2_ts
                if spo2_age <= _CONTEXT_FRESH_S:
                    sensor_parts.append(f"SpO2={self.spo2_pct}%")
                else:
                    sensor_parts.append(
                        f"SpO2={self.spo2_pct}% (stale, {int(spo2_age)}s ago — do NOT report as current)"
                    )
        if self.skin_temperature_c:
            sensor_parts.append(f"Temp={self.skin_temperature_c}°C")
        if self.activity_state and self.activity_state != "unknown":
            sensor_parts.append(f"State={self.activity_state}")
        if self.ambient_light_lux:
            sensor_parts.append(f"Light={self.ambient_light_lux}lux")
        if self.battery_pct < 100:
            sensor_parts.append(f"Battery={self.battery_pct}%")
        if sensor_parts:
            sections.append("Sensors: " + " | ".join(sensor_parts))

        # Adaptive behavior hints — only fire on FRESH HR; otherwise
        # they encourage the model to assume an elevated state that
        # may be hours old.
        hr_fresh = self.heart_rate and (
            float(getattr(self, "heart_rate_sample_ts", 0.0) or 0.0) > 0
            and (now - float(self.heart_rate_sample_ts)) <= _CONTEXT_FRESH_S
        )
        if hr_fresh and self.heart_rate > 140:
            sections.append("USER ALERT: Heart rate critically high. Be extremely concise.")
        elif hr_fresh and self.heart_rate > 100:
            sections.append("User's heart rate is elevated. Keep responses brief.")

        # Head pose
        if any(abs(v) > 15 for v in self.head_pose):
            sections.append(f"Head pose (pitch/roll/yaw): {[round(v, 1) for v in self.head_pose]}")

        # Location
        if self.location:
            sections.append(f"Location: lat={self.location.get('lat')}, lon={self.location.get('lon')}")

        # Audio context
        if self.audio_ambient and self.audio_ambient != "silence":
            sections.append(f"Audio environment: {self.audio_ambient}")

        # Vision context
        if self.has_vision:
            sections.append("Camera feed is active — a frame is attached to this message.")
            if self.scene_description:
                sections.append(f"Scene: {self.scene_description}")
            if self.detected_objects:
                sections.append(f"Objects: {', '.join(self.detected_objects[:10])}")
            if self.text_in_scene:
                sections.append(f"Text visible: {', '.join(self.text_in_scene[:5])}")

        # Gesture
        if self.gesture:
            sections.append(f"Gesture detected: {self.gesture}")

        # Nodes
        if self.connected_nodes:
            sections.append(f"Connected nodes: {', '.join(self.connected_nodes)}")

        return "\n".join(sections) if sections else "No sensor data available."

    def to_llm_user_content(self, text: str) -> dict | list:
        """
        Build the user message content, optionally attaching a vision frame
        as an OpenAI-compatible image_url content block.
        """
        if self.has_vision and self.vision_data_url:
            return [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": self.vision_data_url, "detail": "low"}},
            ]
        return text


class PerceptionEngine:
    """
    Maintains the latest PerceptionFrame for each session by fusing
    data from all input streams (telemetry, vision, audio).
    """

    def __init__(self):
        self._frames: dict[str, PerceptionFrame] = {}

    def get_frame(self, session_id: str) -> PerceptionFrame:
        if session_id not in self._frames:
            self._frames[session_id] = PerceptionFrame()
        return self._frames[session_id]

    @staticmethod
    def _first_valid(*values):
        """Return the first value that is not None, preserving valid zeros."""
        for v in values:
            if v is not None:
                return v
        return None

    def update_sensors(self, session_id: str, sensors: dict):
        """Update the perception frame with structured telemetry data."""
        frame = self.get_frame(session_id)
        frame.timestamp = time.time()

        vitals = sensors.get("vitals", {})
        imu = sensors.get("imu", {})
        env = sensors.get("environment", {})
        device = sensors.get("device", {})
        inferred = sensors.get("inferred_state", "")

        _fv = self._first_valid

        # Vitals — support both structured and flat
        _hr = _fv(vitals.get("ppg_heart_rate"), sensors.get("ppg_heart_rate"), sensors.get("heart_rate_bpm"))
        if _hr is not None:
            # Freshness contract (operator report 2026-05-09 round 2):
            # senders MUST supply an explicit ``*_sample_ts``. iOS
            # HealthKitAdapter forwards ``HKQuantitySample.endDate``;
            # JWBleAdapterWired forwards ``Date().timeIntervalSince1970``
            # (the W300 spot test just returned, so `Date()` IS the
            # sample time). If a sender omits the field, we fall back
            # to ``0.0`` (= "never seen") which makes the proactive
            # engine treat the sample as STALE — old-build clients
            # CANNOT smuggle a fake-fresh reading by silently leaning
            # on a brain-side ``time.time()`` default.
            _hr_ts_raw = _fv(
                vitals.get("ppg_heart_rate_sample_ts"),
                sensors.get("ppg_heart_rate_sample_ts"),
                sensors.get("heart_rate_sample_ts"),
            )
            new_hr_ts = float(_hr_ts_raw) if _hr_ts_raw is not None else 0.0
            _hr_src = _fv(
                vitals.get("ppg_heart_rate_source"),
                sensors.get("source"),
                sensors.get("heart_rate_source"),
            )
            new_hr_source = str(_hr_src) if _hr_src is not None else ""

            # 2026-06-05 demo prep — Source priority. The per-update
            # path used to unconditionally overwrite ``frame.heart_rate``,
            # which let an Apple HealthKit emit (cached "resting HR" from
            # hours ago) clobber a live W300 / Veepoo BPM that landed
            # microseconds earlier. Now: a live wearable's fresh sample
            # wins over any HealthKit / cloud-mirror value, regardless
            # of arrival order. HealthKit can still update the slot when
            # no live wearable has reported, but it cannot demote a
            # currently-live wearable reading.
            now = time.time()
            new_is_live_wearable = _is_live_wearable(new_hr_source)
            new_is_lagging = _is_lagging_source(new_hr_source)
            new_is_fresh = new_hr_ts > 0 and (now - new_hr_ts) <= _CONTEXT_FRESH_S
            cur_source = (frame.heart_rate_source or "").strip().lower()
            cur_ts = float(getattr(frame, "heart_rate_sample_ts", 0.0) or 0.0)
            cur_is_live_wearable = _is_live_wearable(cur_source)
            cur_is_fresh = cur_ts > 0 and (now - cur_ts) <= _CONTEXT_FRESH_S

            should_update = True
            if frame.heart_rate and cur_is_live_wearable and cur_is_fresh:
                # Currently displaying a fresh wearable HR. A lagging
                # source (HealthKit / cloud mirror) MUST NOT demote it,
                # even if its arrival timestamp is technically newer.
                if new_is_lagging or (not new_is_live_wearable and not new_is_fresh):
                    should_update = False
                # Wearable→wearable: keep the freshest sample.
                elif new_is_live_wearable and new_hr_ts < cur_ts:
                    should_update = False

            if should_update:
                frame.heart_rate = _hr
                frame.heart_rate_sample_ts = new_hr_ts
                if new_hr_source:
                    frame.heart_rate_source = new_hr_source
        _spo2 = _fv(vitals.get("spo2_pct"), sensors.get("spo2_pct"))
        if _spo2 is not None:
            _spo2_ts_raw = _fv(
                vitals.get("spo2_sample_ts"),
                sensors.get("spo2_sample_ts"),
            )
            new_spo2_ts = float(_spo2_ts_raw) if _spo2_ts_raw is not None else 0.0
            _spo2_src = _fv(
                vitals.get("spo2_source"),
                sensors.get("source"),
                sensors.get("spo2_source"),
            )
            new_spo2_source = str(_spo2_src) if _spo2_src is not None else ""

            # Same source-priority rule as heart rate (see above).
            now2 = time.time()
            new_spo2_is_live = _is_live_wearable(new_spo2_source)
            new_spo2_is_lagging = _is_lagging_source(new_spo2_source)
            new_spo2_is_fresh = new_spo2_ts > 0 and (now2 - new_spo2_ts) <= _CONTEXT_FRESH_S
            cur_spo2_source = (frame.spo2_source or "").strip().lower()
            cur_spo2_ts = float(getattr(frame, "spo2_sample_ts", 0.0) or 0.0)
            cur_spo2_is_live = _is_live_wearable(cur_spo2_source)
            cur_spo2_is_fresh = cur_spo2_ts > 0 and (now2 - cur_spo2_ts) <= _CONTEXT_FRESH_S

            should_update_spo2 = True
            if frame.spo2_pct and cur_spo2_is_live and cur_spo2_is_fresh:
                if new_spo2_is_lagging or (not new_spo2_is_live and not new_spo2_is_fresh):
                    should_update_spo2 = False
                elif new_spo2_is_live and new_spo2_ts < cur_spo2_ts:
                    should_update_spo2 = False

            if should_update_spo2:
                frame.spo2_pct = _spo2
                frame.spo2_sample_ts = new_spo2_ts
                if new_spo2_source:
                    frame.spo2_source = new_spo2_source
        _temp = vitals.get("skin_temperature_c")
        if _temp is not None:
            frame.skin_temperature_c = _temp

        # IMU
        _pose = imu.get("head_pose_euler")
        if _pose is not None:
            frame.head_pose = _pose

        # Environment
        _lux = env.get("ambient_light_lux")
        if _lux is not None:
            frame.ambient_light_lux = _lux

        # Device
        _batt = _fv(device.get("battery_pct"), sensors.get("battery_pct"))
        if _batt is not None:
            frame.battery_pct = _batt

        # Inferred state
        if inferred:
            frame.activity_state = inferred

        # Location (from biometric or GPS source)
        gps = sensors.get("gps")
        if gps:
            frame.location = gps

    def update_vision(self, session_id: str, vision_buffer: "VisionBuffer", node_id: str):
        """Update vision data from the latest frame in the buffer."""
        frame = self.get_frame(session_id)
        data_url = vision_buffer.latest_data_url(node_id)
        if data_url:
            frame.has_vision = True
            frame.vision_data_url = data_url
            frame.timestamp = time.time()

            latest = vision_buffer.latest(node_id)
            if latest:
                meta = latest.get("metadata", {})
                frame.scene_description = meta.get("scene_description", "")
                frame.detected_objects = meta.get("detected_objects", [])
                frame.text_in_scene = meta.get("text_in_scene", [])

    def update_audio_context(self, session_id: str, ambient: str = "", transcript: str = ""):
        frame = self.get_frame(session_id)
        if ambient:
            frame.audio_ambient = ambient
        if transcript:
            frame.transcript = transcript

    def update_gesture(self, session_id: str, gesture: str):
        frame = self.get_frame(session_id)
        frame.gesture = gesture
        frame.timestamp = time.time()

    def update_connected_nodes(self, session_id: str, nodes: list[str]):
        frame = self.get_frame(session_id)
        frame.connected_nodes = nodes

    def clear(self, session_id: str):
        self._frames.pop(session_id, None)
