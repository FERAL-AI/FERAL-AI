"""
FERAL Scene Understanding — Multi-Provider VLM Pipeline
==========================================================
Analyzes camera frames via a VLM to produce structured scene
descriptions that feed into the PerceptionFrame.

Supports multiple VLM providers:
  - openai   (GPT-4o, default)
  - gemini   (Gemini 2.0 Flash — fast/cheap)
  - ollama   (LLaVA, Moondream — local/private)

Multiple analysis modes:
  - General scene analysis
  - Object tracking (what changed since last frame)
  - Text extraction (OCR mode)
  - Multi-frame reasoning (motion/activity)
"""

from __future__ import annotations
import json
import logging
import os
import time
from typing import Optional, TYPE_CHECKING

from config.runtime import ollama_base_url

if TYPE_CHECKING:
    from agents.llm_provider import LLMProvider

logger = logging.getLogger("feral.scene")

# ─────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────

SCENE_ANALYSIS_PROMPT = """You are the vision system of a wearable AI operating system (smart glasses).
Analyze this camera frame and return a JSON object with these fields:
- "scene_description": one sentence describing the scene and user's likely activity
- "detected_objects": array of up to 10 notable objects visible
- "text_in_scene": array of any readable text (signs, screens, labels)
- "people_count": integer count of people visible
- "ambient": one of "indoor_quiet", "indoor_crowded", "outdoor_urban", "outdoor_nature", "vehicle", "workspace", "unknown"

Return ONLY valid JSON. No markdown, no explanation."""

OBJECT_TRACKING_PROMPT = """You are the vision system of a wearable AI. Compare this frame to the previous context.
Previous scene: {previous_description}

Analyze the CURRENT frame and return a JSON object:
- "scene_description": what the scene looks like now
- "changes": array of changes from the previous scene (e.g. "person left", "new object on table")
- "detected_objects": array of up to 10 objects visible now
- "text_in_scene": array of readable text
- "motion_detected": true/false if significant movement occurred
- "activity": what the user appears to be doing

Return ONLY valid JSON."""

TEXT_EXTRACTION_PROMPT = """You are an OCR system built into smart glasses.
Extract ALL readable text from this image. Return a JSON object:
- "text_blocks": array of objects, each with "text" (the content) and "location" (top/center/bottom/left/right)
- "primary_content": the most important text visible (e.g. a sign, document title, screen content)
- "language": detected language of the text

Return ONLY valid JSON."""

MULTI_FRAME_PROMPT = """You are analyzing a sequence of {count} camera frames from smart glasses.
The frames are ordered chronologically. Describe what happened across these frames:
- "activity_summary": one sentence describing what the user did across these frames
- "motion_direction": where things moved (left, right, approaching, receding, stationary)
- "scene_transition": did the scene change significantly? (same_scene, minor_change, new_location)
- "key_events": array of notable events observed across frames

Return ONLY valid JSON."""


# How many consecutive connection failures mark the provider unreachable.
# Three, not one: a VLM that is up can still refuse a single call (a model
# still loading, a socket lost mid-request), and one unlucky tick must not
# take vision away from a working install.
UNREACHABLE_AFTER_FAILURES = 3

# Backoff between probes once the provider IS marked unreachable. Doubles
# from the first value, capped at the last.
#
# The numbers come from one morning on the operator's brain: ScreenLoop
# ticks every 8s, the scene cooldown is 10s, so a dead Ollama produced a
# call every ~16s, and ``feral.scene`` logged 78 ERROR plus 78 WARNING
# lines in a single morning, still climbing after a restart that night.
# None of them said anything the first one had not. A provider that has
# refused three connections in a row is not going to answer the fourth
# eight seconds later; it will answer when the operator starts it, and
# ten minutes is a fine granularity for noticing that.
UNREACHABLE_BACKOFF_START_S = 30.0
UNREACHABLE_BACKOFF_MAX_S = 600.0

# Exception types that mean "nothing is listening", as opposed to "the
# model rejected the request". Matched by name so this module does not
# have to import httpx, and widened by the message test below because
# httpx wraps the whole retry set in ``ConnectError: All connection
# attempts failed``, which is the exact text in the operator's log.
_CONNECTION_ERROR_NAMES = (
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "PoolTimeout",
    "ConnectionError",
    "ConnectionRefusedError",
    "TimeoutError",
    "OSError",
)
_CONNECTION_ERROR_PHRASES = (
    "all connection attempts failed",
    "connection refused",
    "connect call failed",
    "nodename nor servname",
    "name or service not known",
    "cannot connect to host",
    "timed out",
)


def is_connection_error(exc: BaseException) -> bool:
    """Is this "nothing is listening" rather than "the model said no"?

    A 401 or a 400 from a live provider is a configuration problem the
    operator must see every time. A refused TCP connection is a fact that
    stays true until something changes, and repeating it every 16 seconds
    is noise that buries everything else in the log.
    """
    name = type(exc).__name__
    if name in _CONNECTION_ERROR_NAMES:
        return True
    text = str(exc).lower()
    return any(phrase in text for phrase in _CONNECTION_ERROR_PHRASES)


class SceneAnalyzer:
    """
    Analyzes vision frames through a VLM with support for multiple
    providers, analysis modes, and multi-frame reasoning.
    """

    def __init__(self, llm: "LLMProvider" = None):
        self._llm = llm
        self._vlm_provider = os.getenv("FERAL_VLM_PROVIDER", "")
        self._vlm_model = os.getenv("FERAL_VLM_MODEL", "")
        self._vlm_base_url = os.getenv("FERAL_VLM_BASE_URL", "")
        self._vlm_api_key = os.getenv("FERAL_VLM_API_KEY", "")

        self._vlm_client = None
        self._init_vlm_client()

        self._last_analysis: dict[str, float] = {}
        self._cooldown = 10.0
        self._cache: dict[str, dict] = {}
        self._history: dict[str, list[dict]] = {}
        self._max_history = 5
        # Set once per model the first time we have to salvage a prose
        # reply, so the "this VLM ignores the JSON contract" warning is
        # logged loudly once instead of every ``_cooldown`` seconds.
        self._prose_fallback_warned: set[str] = set()

        # Reachability state. See UNREACHABLE_AFTER_FAILURES.
        self._connect_failures = 0
        self._unreachable_since = 0.0
        self._unreachable_detail = ""
        self._next_probe_at = 0.0
        self._backoff_s = UNREACHABLE_BACKOFF_START_S

    def _init_vlm_client(self):
        """Initialize a dedicated VLM client if a separate provider is configured."""
        if not self._vlm_provider:
            return

        import httpx

        if self._vlm_provider == "gemini":
            api_key = self._vlm_api_key or os.getenv("GEMINI_API_KEY", "")
            if api_key:
                self._vlm_client = {
                    "type": "gemini",
                    "api_key": api_key,
                    "model": self._vlm_model or "gemini-2.0-flash",
                    "http": httpx.AsyncClient(timeout=30.0),
                }
                logger.info(f"VLM: Gemini ({self._vlm_client['model']})")
        elif self._vlm_provider == "ollama":
            base = self._vlm_base_url or ollama_base_url()
            model = self._vlm_model or "llava"
            self._vlm_client = {
                "type": "ollama",
                "base_url": base,
                "model": model,
                "http": httpx.AsyncClient(base_url=f"{base}/v1", timeout=60.0),
            }
            if not any(h in model.lower() for h in ("llava", "moondream", "qwen2-vl", "minicpm-v", "bakllava", "gemma3")):
                logger.warning(
                    "Ollama VLM model '%s' may not support vision. Recommended: llava or moondream.",
                    model,
                )
            logger.info(f"VLM: Ollama ({self._vlm_client['model']})")

    @property
    def configured(self) -> bool:
        """Is a VLM wired at all? True whenever a client object exists."""
        if self._vlm_client:
            return True
        return self._llm is not None and self._llm.available

    @property
    def unreachable(self) -> bool:
        """Has the provider refused enough connections to be written off,
        and is it still inside its backoff window?"""
        if not self._unreachable_since:
            return False
        return time.time() < self._next_probe_at

    @property
    def available(self) -> bool:
        """Can a call plausibly succeed right now?

        This used to be ``configured`` alone: true whenever a client
        object existed, which is true of a client pointed at an Ollama
        that is not running. ScreenLoop reads this before every tick, so
        "a client exists" meant "keep calling forever". A provider inside
        its backoff window now answers False, which is what stops the
        loop rather than merely slowing it down.
        """
        return self.configured and not self.unreachable

    @property
    def provider_health(self) -> dict:
        """Reachability, for anywhere provider health is reported."""
        health = {
            "provider": self._describe_vlm(),
            "configured": self.configured,
            "available": self.available,
            "unreachable": self.unreachable,
            "consecutive_connection_failures": self._connect_failures,
        }
        if self._unreachable_since:
            health["unreachable_since"] = self._unreachable_since
            health["detail"] = self._unreachable_detail
            health["next_probe_in_s"] = max(
                0.0, round(self._next_probe_at - time.time(), 1),
            )
        return health

    def _note_vlm_reachable(self) -> None:
        """A call got through. Clear the backoff."""
        if self._unreachable_since:
            logger.info(
                "Vision provider %s is reachable again after %.0fs",
                self._describe_vlm(), time.time() - self._unreachable_since,
            )
        self._connect_failures = 0
        self._unreachable_since = 0.0
        self._unreachable_detail = ""
        self._next_probe_at = 0.0
        self._backoff_s = UNREACHABLE_BACKOFF_START_S

    def _note_vlm_connection_failure(self, exc: BaseException) -> None:
        """A call could not reach the provider.

        Under the threshold this stays at debug; the caller has already
        logged the individual failure. At and over it, the provider is
        marked unreachable and the next probe is pushed out by an
        exponential backoff capped at :data:`UNREACHABLE_BACKOFF_MAX_S`,
        with ONE warning per transition instead of an ERROR per tick.
        """
        self._connect_failures += 1
        self._unreachable_detail = str(exc)[:200]
        if self._connect_failures < UNREACHABLE_AFTER_FAILURES:
            logger.debug(
                "Vision provider %s connection failure %d/%d: %s",
                self._describe_vlm(), self._connect_failures,
                UNREACHABLE_AFTER_FAILURES, self._unreachable_detail,
            )
            return

        first_time = not self._unreachable_since
        now = time.time()
        if first_time:
            self._unreachable_since = now
            self._backoff_s = UNREACHABLE_BACKOFF_START_S
        else:
            self._backoff_s = min(
                self._backoff_s * 2.0, UNREACHABLE_BACKOFF_MAX_S,
            )
        self._next_probe_at = now + self._backoff_s
        logger.warning(
            "Vision provider %s is unreachable (%d consecutive connection "
            "failures: %s). Scene analysis is paused; the next probe is in "
            "%.0fs. This is logged once per backoff step, not once per tick.",
            self._describe_vlm(), self._connect_failures,
            self._unreachable_detail, self._backoff_s,
        )

    async def analyze_frame(
        self,
        data_b64: str,
        encoding: str = "jpeg",
        node_id: str = "default",
        force: bool = False,
        mode: str = "general",
        query: str = "",
    ) -> Optional[dict]:
        """
        Analyze a frame via VLM.

        Modes:
          general  — full scene analysis (default)
          tracking — what changed since last frame
          ocr      — extract all text
          query    — answer a specific question about the frame

        ``force`` bypasses the unreachable backoff as well as the
        cooldown, and deliberately so: ``force=True`` is only ever set by
        an explicit request (``screen_capture``, ``perception_query``,
        a test), and an explicit request is exactly the probe the backoff
        is waiting for. The periodic ScreenLoop tick does not force, so it
        keeps backing off.
        """
        if not self.configured:
            return None
        if self.unreachable and not force:
            return None

        now = time.time()
        last = self._last_analysis.get(node_id, 0)
        if not force and (now - last) < self._cooldown:
            return self._cache.get(node_id)

        self._last_analysis[node_id] = now

        prompt = self._select_prompt(mode, node_id, query)
        messages = self._build_vision_messages(prompt, data_b64, encoding)

        try:
            result_text = await self._call_vlm(messages)
            if not result_text:
                if self._connect_failures:
                    # The call never reached the provider, so "returned an
                    # empty reply" is the wrong thing to say about it, and
                    # saying it every tick is the other half of the 78
                    # WARNING lines that came with the 78 ERRORs.
                    # _note_vlm_connection_failure has already reported
                    # this at the right volume.
                    return None
                # Was a bare ``return None``. An empty reply is how a
                # dead/misconfigured VLM presents (the shared LLM's
                # failover chain exhausting on 401s returns "" rather
                # than raising), and swallowing it silently is what let
                # the screen pipeline go quiet for days while every log
                # line still said the loop was running.
                logger.warning(
                    "Scene [%s] [%s]: VLM returned an empty reply (%s), "
                    "no observation produced",
                    mode, node_id, self._describe_vlm(),
                )
                return None

            result = self._parse_json(result_text)
            if result is None:
                result = self._salvage_prose(result_text, mode)
            if result:
                self._cache[node_id] = result
                self._push_history(node_id, result)
                desc = result.get("scene_description", result.get("primary_content", "?"))
                logger.info(f"Scene [{mode}] [{node_id}]: {str(desc)[:60]}")
            else:
                logger.warning(
                    "Scene [%s] [%s]: VLM reply was neither JSON nor usable "
                    "prose (%s), no observation produced",
                    mode, node_id, self._describe_vlm(),
                )
            return result

        except Exception as e:
            logger.warning(f"Scene analysis failed: {e}")
            return None

    async def analyze_with_history(
        self,
        frames: list[dict],
        node_id: str = "default",
    ) -> Optional[dict]:
        """
        Multi-frame reasoning — analyze a sequence of frames together.
        Each frame dict should have 'data_b64' and optionally 'encoding'.
        """
        if not self.configured or self.unreachable or not frames:
            return None

        content_parts = [
            {"type": "text", "text": MULTI_FRAME_PROMPT.format(count=len(frames))},
        ]
        for i, frame in enumerate(frames[:5]):
            b64 = frame.get("data_b64", "")
            enc = frame.get("encoding", "jpeg")
            if b64:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/{enc};base64,{b64}",
                        "detail": "low",
                    },
                })

        messages = [{"role": "user", "content": content_parts}]

        try:
            result_text = await self._call_vlm(messages)
            return self._parse_json(result_text) if result_text else None
        except Exception as e:
            logger.warning(f"Multi-frame analysis failed: {e}")
            return None

    def _select_prompt(self, mode: str, node_id: str, query: str) -> str:
        if mode == "tracking":
            prev = self._cache.get(node_id, {})
            prev_desc = prev.get("scene_description", "No previous scene data.")
            return OBJECT_TRACKING_PROMPT.format(previous_description=prev_desc)
        elif mode == "ocr":
            return TEXT_EXTRACTION_PROMPT
        elif mode == "query" and query:
            return (
                f"You are the vision system of smart glasses. "
                f"Answer this question about what you see: {query}\n"
                f"Return a JSON object with: \"answer\" (your response), "
                f"\"confidence\" (0.0-1.0), \"detected_objects\" (relevant objects)."
            )
        return SCENE_ANALYSIS_PROMPT

    def _build_vision_messages(
        self, prompt: str, data_b64: str, encoding: str,
    ) -> list[dict]:
        mime = f"image/{encoding}"
        return [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{data_b64}",
                    "detail": "low",
                }},
            ],
        }]

    async def _call_vlm(self, messages: list[dict]) -> Optional[str]:
        """Route the VLM call to the appropriate provider.

        Also the single place reachability is decided, so every provider
        adapter shares one definition of "nothing is listening" and the
        backoff cannot be bypassed by adding a third one.
        """
        try:
            if self._vlm_client:
                text = await self._call_dedicated_vlm(messages)
            else:
                text = await self._call_default_llm(messages)
        except Exception as exc:
            if is_connection_error(exc):
                self._note_vlm_connection_failure(exc)
                return None
            raise
        # The call reached the provider. Whatever it answered, the socket
        # worked, so any backoff in progress is over.
        self._note_vlm_reachable()
        return text

    async def _call_default_llm(self, messages: list[dict]) -> Optional[str]:
        """Use the shared LLMProvider (OpenAI-compatible) for vision."""
        response = await self._llm.chat(
            messages=messages, tools=None, temperature=0.1, max_tokens=500,
        )
        text, _ = self._llm.extract_response(response)
        return text

    async def _call_dedicated_vlm(self, messages: list[dict]) -> Optional[str]:
        """Use a separate VLM provider (Gemini, Ollama)."""
        vlm = self._vlm_client
        vlm_type = vlm["type"]

        if vlm_type == "gemini":
            return await self._call_gemini(messages)
        elif vlm_type == "ollama":
            return await self._call_ollama_vlm(messages)
        return None

    async def _call_gemini(self, messages: list[dict]) -> Optional[str]:
        """Call Google Gemini's vision API."""
        vlm = self._vlm_client
        api_key = vlm["api_key"]
        model = vlm["model"]
        http = vlm["http"]

        parts = []
        for msg in messages:
            content = msg.get("content", [])
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        parts.append({"text": block["text"]})
                    elif block.get("type") == "image_url":
                        url = block["image_url"]["url"]
                        if url.startswith("data:"):
                            header, b64_data = url.split(",", 1)
                            mime = header.split(":")[1].split(";")[0]
                            parts.append({
                                "inline_data": {
                                    "mime_type": mime,
                                    "data": b64_data,
                                },
                            })

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{model}:generateContent"
        )
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 500},
        }

        try:
            resp = await http.post(url, json=body, headers={"x-goog-api-key": api_key})
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                return candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        except Exception as e:
            if is_connection_error(e):
                raise  # ``_call_vlm`` owns the backoff; see _call_ollama_vlm.
            logger.error(f"Gemini VLM call failed: {e}")
        return None

    async def _call_ollama_vlm(self, messages: list[dict]) -> Optional[str]:
        """Call Ollama's OpenAI-compatible vision endpoint."""
        vlm = self._vlm_client
        http = vlm["http"]
        model = vlm["model"]

        body = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 500,
            "stream": False,
        }

        try:
            resp = await http.post("/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            if is_connection_error(e):
                # Re-raised so ``_call_vlm`` owns the backoff. Logging it
                # here is what produced 78 ERROR lines in one morning,
                # each one saying "All connection attempts failed" about
                # the same Ollama that was not running.
                raise
            logger.error(f"Ollama VLM call failed: {e}")
        return None

    def _parse_json(self, text: str) -> Optional[dict]:
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.debug("VLM returned non-JSON scene description")
            return None

    def _describe_vlm(self) -> str:
        """Human-readable name of whatever actually served the call."""
        if self._vlm_client:
            return f"{self._vlm_client['type']}/{self._vlm_client.get('model', '?')}"
        return "shared LLM provider"

    def _salvage_prose(self, text: str, mode: str) -> Optional[dict]:
        """Recover a usable observation from a VLM that ignored the JSON contract.

        Every prompt in this module ends with "Return ONLY valid JSON",
        and small local VLMs simply do not comply. ``moondream`` served
        from Ollama answers the scene prompt with a paragraph of prose;
        ``_parse_json`` then returned None, ``analyze_frame`` returned
        None, and ScreenLoop treated a perfectly good caption as "no
        observation". ~9,500 screen episodes stopped dead the day the
        install switched ``vision.provider`` to ollama/moondream, with
        the only trace a debug-level "VLM returned non-JSON" line.

        The caption itself is exactly what the consumers want, so keep
        it rather than discarding it. The structured extras (objects,
        text, counts) genuinely aren't available from a prose reply, so
        they come back empty instead of invented.
        """
        cleaned = (text or "").strip()
        if len(cleaned) < 8:
            # Too short to be a description. A stray "{}", "null" or a
            # truncated token is not an observation.
            return None
        if cleaned[0] in "{[":
            # Looks like it was MEANT to be JSON and came back malformed
            # or truncated. Salvaging that as a caption would put raw
            # braces into episodic memory, so refuse it.
            return None

        model = self._describe_vlm()
        if model not in self._prose_fallback_warned:
            self._prose_fallback_warned.add(model)
            logger.warning(
                "VLM %s does not honour the JSON contract; using its prose "
                "reply as scene_description and leaving structured fields "
                "empty. Switch vision.model to a JSON-capable VLM to get "
                "detected_objects/text_in_scene back.",
                model,
            )

        salvaged: dict = {
            "scene_description": cleaned,
            "detected_objects": [],
            "text_in_scene": [],
            "prose_fallback": True,
        }
        if mode == "ocr":
            salvaged["primary_content"] = cleaned
        if mode == "query":
            # ``_analyze_scene_background`` reads ``answer`` first in
            # query mode, so populate it or the reply reaches nobody.
            salvaged["answer"] = cleaned
            salvaged["confidence"] = 0.5
        return salvaged

    def _push_history(self, node_id: str, result: dict):
        if node_id not in self._history:
            self._history[node_id] = []
        self._history[node_id].append({
            "timestamp": time.time(),
            **result,
        })
        if len(self._history[node_id]) > self._max_history:
            self._history[node_id] = self._history[node_id][-self._max_history:]

    def get_history(self, node_id: str) -> list[dict]:
        return self._history.get(node_id, [])

    def get_cached(self, node_id: str = "default") -> Optional[dict]:
        return self._cache.get(node_id)

    def set_cooldown(self, seconds: float):
        self._cooldown = max(1.0, seconds)
