"""
FERAL Skill Manifest — The Skill Definition Format
=====================================================
This is what developers and companies submit to make their
service available in FERAL. Compatible with MCP, extended
with GenUI hints, flows, and cron/trigger support.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Literal
from uuid import uuid4


class BrandProfile(BaseModel):
    """Visual identity for GenUI rendering."""
    name: str
    primary_color: str = "#007AFF"
    secondary_color: str = "#5856D6"
    logo_url: str = ""
    icon_set: Literal["sf_symbols", "material", "custom"] = "sf_symbols"


class AuthConfig(BaseModel):
    """How the skill authenticates with its backend."""
    type: Literal["none", "api_key", "oauth2", "bearer"]
    api_key_header: Optional[str] = None  # e.g. "X-API-Key"
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    scopes: list[str] = []


class EndpointParam(BaseModel):
    """A parameter for a skill endpoint."""
    name: str
    type: Literal["string", "number", "integer", "boolean", "array", "object"] = "string"
    items: Optional[dict] = None
    required: bool = True
    description: str = ""
    default: Optional[str] = None
    enum: list[str] = []  # Valid values


class SkillEndpoint(BaseModel):
    """A single API endpoint the skill exposes."""
    id: str
    # `method` is a routing *label*. The runtime executor
    # (feral-core/skills/executor.py) uses it to pick a lane:
    #   - WS_EXECUTE -> dispatch to a connected daemon over WebSocket
    #   - GET/POST/PUT/DELETE/PATCH -> generic HTTP runner
    #   - PYTHON / CUSTOM -> resolved by `skill_id` -> Python backing
    #     class in feral-core/skills/impl/*.py; the backing class's
    #     execute(endpoint_id, args, vault) routes by `endpoint_id`,
    #     not by `method`. Do not add `endpoint.method == ...` branches
    #     inside impl/ modules — see tests/test_skill_method_is_metadata.py.
    method: Literal[
        "GET", "POST", "PUT", "DELETE", "PATCH",
        "WS_EXECUTE", "PYTHON", "CUSTOM",
    ] = "GET"
    url: str
    description: str  # Natural language — this is what the LLM reads
    params: list[EndpointParam] = []
    returns_description: str = ""  # What the response contains
    ui_hint: Optional[str] = None  # "grid_cards", "detail_card", "map", "list", "metric"
    # If True, this endpoint must run in an execution sandbox and must not
    # silently fall back to host in-process execution.
    requires_sandbox: bool = False
    # Manifest-level safety metadata. When present these supersede the
    # name-substring heuristics in tool_runner.classify_safety so the
    # policy decision is grounded in the skill author's declared intent
    # instead of "does the endpoint name contain 'delete'".
    #
    # * safety_tier: "safe" -> AUTO, "confirm" -> CONFIRM, "deny" -> DENY.
    # * read_only_hint: marks an endpoint as side-effect-free; strict-mode
    #   autonomy treats read-only endpoints as AUTO without an approval
    #   prompt.
    # * requires_user_approval: hard override that forces CONFIRM even
    #   when the substring heuristic would have auto-approved.
    safety_tier: Optional[Literal["safe", "confirm", "deny"]] = None
    read_only_hint: bool = False
    requires_user_approval: bool = False
    # How much of this endpoint's result may reach the model, as a tier
    # name from `skills/result_budget.py` ("standard" / "feed" /
    # "workspace"). Overrides the manifest-level declaration. Unset = the
    # manifest's value, else "standard".
    #
    # This exists because the budget is a property of the TOOL, not a
    # global: before v2026.7.30 every result was cut to 2 000 chars / 20
    # list items, which made `coding_tools__read_file` return ~30 lines of
    # source no matter what `limit` the model asked for. Declare it per
    # endpoint, not per skill, so a first-party skill can keep a tight
    # bound on the one endpoint that relays third-party bytes (e.g.
    # `coding_tools__web_fetch`).
    #
    # Only honoured for manifests that ship in `feral-core/skills/manifests/`;
    # runtime-installed skills are clamped to "standard" whatever they claim.
    result_budget: Optional[str] = None


class FlowStep(BaseModel):
    """One step in a multi-step flow."""
    endpoint_id: str
    condition: Optional[str] = None  # e.g. "result.rain_probability > 0.5"
    then_endpoint_id: Optional[str] = None


class SkillFlow(BaseModel):
    """A multi-step orchestration (e.g., search → select → order → confirm)."""
    id: str
    description: str
    steps: list[FlowStep] = []


class CronDefinition(BaseModel):
    """A scheduled background job."""
    id: str
    schedule: str  # cron format: "0 8 * * 1-5" = weekdays at 8am
    flow_id: Optional[str] = None  # Execute a flow on schedule
    endpoint_id: Optional[str] = None  # Or execute a single endpoint


class TriggerDefinition(BaseModel):
    """An event-driven reaction."""
    id: str
    condition: str  # e.g. "biometric.heart_rate_bpm > 150"
    action_flow_id: Optional[str] = None
    action_endpoint_id: Optional[str] = None
    cooldown_seconds: int = 300  # Don't re-trigger for 5 min


class SkillManifest(BaseModel):
    """
    The complete skill definition.
    This is what developers submit to register a skill with FERAL.
    MCP-compatible at the tool level, extended with GenUI + flows.
    """
    skill_id: str = Field(default_factory=lambda: str(uuid4()))
    version: str = "1.0.0"
    author: str = ""
    
    # Brand & Discovery
    brand: BrandProfile
    description: str  # Natural language description for the LLM
    trigger_phrases: list[str] = []  # Hint phrases that activate this skill
    categories: list[str] = []  # ["food", "transport", "productivity", ...]
    
    # Auth
    auth: AuthConfig = AuthConfig(type="none")
    
    # Endpoints (individual capabilities)
    endpoints: list[SkillEndpoint] = []
    
    # Flows (multi-step orchestrations)
    flows: list[SkillFlow] = []
    
    # Background jobs
    crons: list[CronDefinition] = []
    
    # Event triggers
    triggers: list[TriggerDefinition] = []
    
    # Permissions
    permissions: list[str] = []  # ["location", "contacts", "camera", "messaging"]
    
    # Hardware requirements (for hardware skills)
    requires_daemon: bool = False
    daemon_node_type: Optional[str] = None  # "desktop", "rpi", "robot"
    # If True, every endpoint in this skill must execute inside a sandbox.
    # Runtime enforcement lives in `skills/executor.py`.
    requires_sandbox: bool = False
    
    # Rate limits
    max_calls_per_hour: int = 1000

    # Default result budget tier for every endpoint in this skill; see
    # SkillEndpoint.result_budget and skills/result_budget.py.
    result_budget: Optional[str] = None


# ─────────────────────────────────────────────
# Example: Weather Skill
# ─────────────────────────────────────────────

WEATHER_SKILL = SkillManifest(
    skill_id="weather_current",
    version="1.0.0",
    author="feral-core",
    brand=BrandProfile(
        name="Weather",
        primary_color="#4A90D9",
        logo_url="",
        icon_set="sf_symbols",
    ),
    description="Get current weather conditions and forecasts for any location.",
    trigger_phrases=[
        "what's the weather",
        "is it going to rain",
        "temperature outside",
        "weather forecast",
        "do I need an umbrella",
        "how hot is it",
    ],
    categories=["weather", "utility"],
    auth=AuthConfig(type="api_key", api_key_header="appid"),
    endpoints=[
        SkillEndpoint(
            id="current",
            method="GET",
            url="https://api.openweathermap.org/data/2.5/weather",
            description="Get current weather for a location. Returns temperature, conditions, humidity, wind, and coordinates for map display.",
            params=[
                EndpointParam(name="q", type="string", description="City name (e.g. 'London' or 'Cairo,EG')"),
                EndpointParam(name="units", type="string", default="metric", description="Temperature units: metric, imperial, or standard"),
            ],
            returns_description="temp, feels_like, humidity, weather_description, wind_speed, lat, lon",
            ui_hint="map",
        ),
        SkillEndpoint(
            id="forecast",
            method="GET",
            url="https://api.openweathermap.org/data/2.5/forecast",
            description="Get multi-day forecast (up to 3 days summarized). Returns daily highs, lows, and conditions with coordinates.",
            params=[
                EndpointParam(name="q", type="string", description="City name (e.g. 'London' or 'Cairo,EG')"),
                EndpointParam(name="units", type="string", default="metric", description="Temperature units"),
            ],
            returns_description="days: array of {date, temp_high, temp_low, conditions}, lat, lon",
            ui_hint="list",
        ),
    ],
)
