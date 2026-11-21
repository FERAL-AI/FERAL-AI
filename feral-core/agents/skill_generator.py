"""
FERAL Skill Generator — Self-Evolving Agent Intelligence
============================================================
The agent can propose, generate, and register new skills at runtime
based on user conversations. This is the feature that makes feral
fundamentally different from every other agent system.

Flow:
  1. User says: "I wish you could check my Notion pages"
  2. Agent detects an unmet capability need
  3. Agent generates a skill manifest (JSON) using LLM
  4. Brain sends a CONFIRM request to the client
  5. User approves (or edits) the skill
  6. Skill is registered live — no restart needed
  7. Agent can now use it in future conversations

This is not "tool use" — this is tool *creation*.
"""

from __future__ import annotations
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from config.loader import feral_home

if TYPE_CHECKING:
    from agents.llm_provider import LLMProvider
    from skills.registry import SkillRegistry

logger = logging.getLogger("feral.skill_gen")

SKILL_GENERATION_PROMPT = """You are FERAL's skill architect. The user needs a capability that doesn't exist yet.
Based on the conversation, generate a skill manifest JSON that would satisfy the user's need.

The skill manifest format:
{
  "skill_id": "lowercase_snake_case",
  "brand": {
    "name": "Human Readable Name",
    "primary_color": "#hexcolor"
  },
  "description": "What this skill does — the LLM reads this to decide when to use it",
  "trigger_phrases": ["phrase 1", "phrase 2", "phrase 3"],
  "categories": ["category1", "category2"],
  "auth": {
    "type": "none" | "api_key" | "bearer" | "oauth2",
    "api_key_header": "X-API-Key" (if api_key)
  },
  "endpoints": [
    {
      "id": "endpoint_id",
      "method": "GET" | "POST" | "PUT" | "DELETE",
      "url": "https://api.example.com/v1/endpoint",
      "description": "What this endpoint does",
      "params": [
        {
          "name": "param_name",
          "type": "string" | "number" | "boolean",
          "description": "What this param is",
          "required": true
        }
      ],
      "returns_description": "What the response contains",
      "ui_hint": "list" | "detail_card" | "metric" | "grid_cards"
    }
  ],
  "requires_daemon": false,
  "permissions": []
}

Rules:
- Use REAL API endpoints when possible (you know many public APIs)
- If the API requires auth, set the auth type correctly
- Make trigger_phrases natural and varied
- Keep endpoint descriptions clear — the LLM uses them for routing
- If the capability can be done LOCALLY (shell commands, Python scripts, file operations,
  audio generation, etc.) instead of via an external API, add a "python_impl" field with
  a complete Python class that extends BaseSkill. Format:

  "python_impl": "from skills.base import BaseSkill\\nimport subprocess\\n\\nclass MySkill(BaseSkill):\\n    def __init__(self):\\n        super().__init__(skill_id='my_skill')\\n\\n    async def execute(self, endpoint_id, args, vault):\\n        # implementation\\n        return {'success': True, 'status_code': 200, 'data': {...}, 'error': None}"

  For local skills, set endpoint URLs to "internal://skill_id/endpoint_id" and method to "PYTHON".

Return ONLY the JSON. No markdown, no explanation."""

DETECT_NEED_PROMPT = """Analyze this conversation snippet. Does the user want the AI to do something it currently cannot?

Current capabilities (skills already loaded):
{skills}

Conversation:
{conversation}

If the user is asking for something no existing skill can handle, respond with:
{{"needs_skill": true, "capability": "brief description of what's needed", "service": "the service/API that could provide it", "confidence": 0.0-1.0}}

If existing skills can handle it, respond with:
{{"needs_skill": false, "confidence": 0.0-1.0}}

Return ONLY JSON."""


class SkillGenerator:
    """
    Detects unmet capability needs and generates new skills at runtime.
    """

    def __init__(
        self,
        llm: "LLMProvider",
        skill_registry: "SkillRegistry",
        skills_dir: Optional[str] = None,
    ):
        self._llm = llm
        self._registry = skill_registry
        self._skills_dir = Path(skills_dir) if skills_dir else self._default_skills_dir()
        self._pending_skills: dict[str, dict] = {}  # skill_id → manifest dict
        self._generation_count = 0
        self._last_detection_time = 0
        self._detection_cooldown = 30  # seconds between detection attempts
        self._confidence_threshold = 0.72
        self._need_debounce_seconds = 600
        self._need_min_hits = 2
        self._proposal_cooldown_seconds = 1800
        self._need_hits: dict[str, dict] = {}
        self._last_proposal_at: dict[str, float] = {}

    @staticmethod
    def _default_skills_dir() -> Path:
        return feral_home() / "skills"

    @staticmethod
    def _normalize_capability_key(capability: str) -> str:
        normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in (capability or ""))
        return " ".join(normalized.split())[:160]

    async def detect_unmet_need(self, conversation: list[dict], *, call_site: str | None = None) -> Optional[dict]:
        """
        Analyze recent conversation to detect if the user needs a capability
        that doesn't exist yet.
        """
        now = time.time()
        if now - self._last_detection_time < self._detection_cooldown:
            return None
        self._last_detection_time = now

        if not self._llm or not self._llm.available:
            return None

        skill_names = [
            f"{s.skill_id}: {s.description[:80]}"
            for s in self._registry.skills.values()
        ]
        skills_str = "\n".join(skill_names) if skill_names else "(none)"

        recent = conversation[-6:] if len(conversation) > 6 else conversation
        conv_str = "\n".join(
            f"{m.get('role', 'user')}: {(m.get('text') or m.get('content') or '')[:200]}"
            for m in recent
        )

        prompt = DETECT_NEED_PROMPT.format(skills=skills_str, conversation=conv_str)
        call_options: dict[str, Any] = {"call_site": call_site} if call_site is not None else {}

        try:
            response = await self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=200,
                **call_options,
            )
            text, _ = self._llm.extract_response(response)
            if not text:
                return None

            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
            result = json.loads(cleaned)
            if result.get("needs_skill"):
                capability = str(result.get("capability", "")).strip()
                try:
                    confidence = float(result.get("confidence", 0.5) or 0.5)
                except (TypeError, ValueError):
                    confidence = 0.5
                if not capability:
                    return None
                if confidence < self._confidence_threshold:
                    logger.debug(
                        "Skipping unmet need due to low confidence %.2f < %.2f for '%s'",
                        confidence,
                        self._confidence_threshold,
                        capability,
                    )
                    return None

                key = self._normalize_capability_key(capability)
                last = self._need_hits.get(key, {"count": 0, "last_seen": 0.0})
                if now - float(last.get("last_seen", 0.0)) <= self._need_debounce_seconds:
                    count = int(last.get("count", 0)) + 1
                else:
                    count = 1
                self._need_hits[key] = {"count": count, "last_seen": now}
                if count < self._need_min_hits:
                    logger.debug("Debounced unmet need '%s' (hit %s/%s)", capability, count, self._need_min_hits)
                    return None

                last_proposal = self._last_proposal_at.get(key, 0.0)
                if now - last_proposal < self._proposal_cooldown_seconds:
                    logger.debug("Skipping unmet need '%s' due to proposal cooldown", capability)
                    return None

                self._last_proposal_at[key] = now
                result["confidence"] = confidence
                result["capability_key"] = key
                return result
            return None

        except Exception as e:
            logger.debug(f"Need detection failed: {e}")
            return None

    async def generate_skill(self, capability: str, service: str = "", *, call_site: str | None = None) -> Optional[dict]:
        """
        Generate a new skill manifest using LLM based on what's needed.
        Returns the manifest dict or None.
        """
        if not self._llm or not self._llm.available:
            return None

        context = f"The user needs: {capability}"
        if service:
            context += f"\nSuggested service/API: {service}"
        call_options: dict[str, Any] = {"call_site": call_site} if call_site is not None else {}

        try:
            response = await self._llm.chat(
                messages=[
                    {"role": "system", "content": SKILL_GENERATION_PROMPT},
                    {"role": "user", "content": context},
                ],
                temperature=0.3,
                max_tokens=1500,
                **call_options,
            )
            text, _ = self._llm.extract_response(response)
            if not text:
                return None

            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]

            manifest = json.loads(cleaned.strip())

            if "skill_id" not in manifest or "endpoints" not in manifest:
                logger.warning("Generated skill missing required fields")
                return None

            # Store as pending — needs user approval
            skill_id = manifest["skill_id"]
            self._pending_skills[skill_id] = manifest
            self._generation_count += 1
            logger.info(f"Generated skill: {skill_id} ({manifest.get('brand', {}).get('name', '?')})")
            return manifest

        except json.JSONDecodeError:
            logger.warning("LLM returned invalid JSON for skill generation")
            return None
        except Exception as e:
            logger.warning(f"Skill generation failed: {e}")
            return None

    async def approve_skill(self, skill_id: str) -> bool:
        """
        User approved a pending skill. Register it live and persist to disk.
        Writes to {skills_dir}/{skill_id}/manifest.json so _load_marketplace_skills()
        can reload it on restart.
        """
        manifest = self._pending_skills.pop(skill_id, None)
        if not manifest:
            logger.warning(f"No pending skill with id: {skill_id}")
            return False

        try:
            from models.skill_manifest import SkillManifest
            skill = SkillManifest(**manifest)
            self._registry.register_skill(skill)

            skill_dir = self._skills_dir / skill_id
            skill_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = skill_dir / "manifest.json"
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

            python_impl = manifest.pop("python_impl", None)
            if python_impl:
                impl_path = skill_dir / "impl.py"
                impl_path.write_text(python_impl)
                logger.info(f"Wrote Python implementation: {impl_path}")

            logger.info(f"Skill approved and registered: {skill_id} → {skill_dir}")
            return True

        except Exception as e:
            logger.error(f"Failed to register approved skill {skill_id}: {e}")
            return False

    def reject_skill(self, skill_id: str) -> bool:
        """User rejected a pending skill."""
        removed = self._pending_skills.pop(skill_id, None)
        if removed:
            logger.info(f"Skill rejected: {skill_id}")
        return removed is not None

    def get_pending_skills(self) -> list[dict]:
        """Get all skills waiting for user approval."""
        return list(self._pending_skills.values())

    async def generate_skill_with_retry(self, capability: str, service: str = "", max_retries: int = 3) -> Optional[dict]:
        """Generate a skill with automatic retry on failure."""
        # Check pre-built templates first for instant demo experience
        template = self._match_template(capability, service)
        if template:
            skill_id = template["skill_id"]
            self._pending_skills[skill_id] = template
            self._generation_count += 1
            logger.info(f"Matched pre-built template: {skill_id}")
            return template

        for attempt in range(max_retries):
            result = await self.generate_skill(capability, service)
            if result:
                valid, errors = self._validate_manifest(result)
                if valid:
                    return result
                logger.warning("Generated skill failed validation (attempt %d/%d): %s", attempt + 1, max_retries, errors)
            else:
                logger.debug("Generation attempt %d/%d returned None", attempt + 1, max_retries)
        logger.error("Skill generation failed after %d retries for: %s", max_retries, capability)
        return None

    @staticmethod
    def _validate_manifest(manifest: dict) -> tuple[bool, list[str]]:
        """Validate a generated skill manifest before registration."""
        errors = []
        if not manifest.get("skill_id"):
            errors.append("missing skill_id")
        if not manifest.get("endpoints"):
            errors.append("missing endpoints")
        sid = manifest.get("skill_id", "")
        if sid and not sid.replace("_", "").isalnum():
            errors.append(f"invalid skill_id format: {sid}")
        for i, ep in enumerate(manifest.get("endpoints", [])):
            if not ep.get("id"):
                errors.append(f"endpoint[{i}] missing id")
            if not ep.get("url") and not ep.get("method", "").upper() == "PYTHON":
                errors.append(f"endpoint[{i}] missing url")
        return (len(errors) == 0, errors)

    @staticmethod
    def _match_template(capability: str, service: str) -> Optional[dict]:
        """Match against pre-built skill templates for reliable demo performance."""
        cap_lower = (capability + " " + service).lower()
        for template in _DEMO_SKILL_TEMPLATES:
            for kw in template.get("_match_keywords", []):
                if kw in cap_lower:
                    result = {k: v for k, v in template.items() if not k.startswith("_")}
                    return result
        return None

    @property
    def stats(self) -> dict:
        return {
            "generated_count": self._generation_count,
            "pending_count": len(self._pending_skills),
            "skills_dir": str(self._skills_dir),
            "confidence_threshold": self._confidence_threshold,
            "debounce_seconds": self._need_debounce_seconds,
            "proposal_cooldown_seconds": self._proposal_cooldown_seconds,
        }


_DEMO_SKILL_TEMPLATES = [
    {
        "_match_keywords": ["github", "pull request", "pr", "repo"],
        "skill_id": "github_pr_review",
        "brand": {"name": "GitHub PR Review", "primary_color": "#24292e"},
        "description": "List and summarize open pull requests from a GitHub repository",
        "trigger_phrases": ["check my PRs", "github pull requests", "show open PRs", "review PRs"],
        "categories": ["developer", "productivity"],
        "auth": {"type": "bearer"},
        "endpoints": [
            {
                "id": "list_prs",
                "method": "GET",
                "url": "https://api.github.com/repos/{owner}/{repo}/pulls",
                "description": "List open pull requests for a repository",
                "params": [
                    {"name": "owner", "type": "string", "description": "Repository owner", "required": True},
                    {"name": "repo", "type": "string", "description": "Repository name", "required": True},
                    {"name": "state", "type": "string", "description": "PR state: open, closed, all", "required": False},
                ],
                "returns_description": "Array of pull request objects with title, number, author, and status",
                "ui_hint": "list",
            },
            {
                "id": "get_pr",
                "method": "GET",
                "url": "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}",
                "description": "Get details of a specific pull request",
                "params": [
                    {"name": "owner", "type": "string", "description": "Repository owner", "required": True},
                    {"name": "repo", "type": "string", "description": "Repository name", "required": True},
                    {"name": "pull_number", "type": "number", "description": "PR number", "required": True},
                ],
                "returns_description": "Full pull request details including diff stats, reviewers, and comments",
                "ui_hint": "detail_card",
            },
        ],
        "requires_daemon": False,
        "permissions": ["network"],
    },
    {
        "_match_keywords": ["weather", "temperature", "forecast", "rain"],
        "skill_id": "weather_lookup",
        "brand": {"name": "Weather Lookup", "primary_color": "#4FC3F7"},
        "description": "Get current weather and forecast for any location",
        "trigger_phrases": ["weather", "temperature", "forecast", "is it going to rain", "what's the weather"],
        "categories": ["utility", "daily"],
        "auth": {"type": "none"},
        "endpoints": [
            {
                "id": "current",
                "method": "GET",
                "url": "https://wttr.in/{location}?format=j1",
                "description": "Get current weather conditions for a location",
                "params": [
                    {"name": "location", "type": "string", "description": "City name or coordinates", "required": True},
                ],
                "returns_description": "Current temperature, humidity, wind, conditions, and 3-day forecast",
                "ui_hint": "metric",
            },
        ],
        "requires_daemon": False,
        "permissions": ["network"],
    },
    {
        "_match_keywords": ["http", "api", "fetch", "request", "endpoint", "curl"],
        "skill_id": "http_request",
        "brand": {"name": "HTTP Request", "primary_color": "#FF7043"},
        "description": "Make HTTP requests to any API endpoint and return the response",
        "trigger_phrases": ["make a request", "call an API", "fetch from URL", "HTTP request"],
        "categories": ["developer", "utility"],
        "auth": {"type": "none"},
        "endpoints": [
            {
                "id": "request",
                "method": "PYTHON",
                "url": "internal://http_request/request",
                "description": "Make an HTTP request to any URL with custom method, headers, and body",
                "params": [
                    {"name": "url", "type": "string", "description": "The URL to request", "required": True},
                    {"name": "method", "type": "string", "description": "HTTP method (GET, POST, PUT, DELETE)", "required": False},
                    {"name": "headers", "type": "string", "description": "JSON string of headers", "required": False},
                    {"name": "body", "type": "string", "description": "Request body", "required": False},
                ],
                "returns_description": "HTTP status code and response body",
                "ui_hint": "detail_card",
            },
        ],
        "python_impl": "from skills.base import BaseSkill\nimport aiohttp\nimport json\n\nclass HttpRequestSkill(BaseSkill):\n    def __init__(self):\n        super().__init__(skill_id='http_request')\n\n    async def execute(self, endpoint_id, args, vault=None):\n        url = args.get('url', '')\n        method = args.get('method', 'GET').upper()\n        headers = json.loads(args.get('headers', '{}')) if args.get('headers') else {}\n        body = args.get('body')\n        try:\n            async with aiohttp.ClientSession() as session:\n                async with session.request(method, url, headers=headers, data=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:\n                    text = await resp.text()\n                    return {'success': True, 'status_code': resp.status, 'data': {'body': text[:5000], 'status': resp.status, 'headers': dict(resp.headers)}, 'error': None}\n        except Exception as e:\n            return {'success': False, 'status_code': 500, 'data': None, 'error': str(e)}",
        "requires_daemon": False,
        "permissions": ["network"],
    },
]
