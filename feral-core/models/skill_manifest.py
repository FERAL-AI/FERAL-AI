"""
FERAL Skill Manifest — The Skill Definition Format
=====================================================
This is what developers and companies submit to make their
service available in FERAL. Compatible with MCP, extended
with GenUI hints, flows, and cron/trigger support.
"""

from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional, Literal
from uuid import uuid4


class SkillPermission(str, Enum):
    """The closed set of capabilities a skill may declare.

    Before this was a vocabulary it was ``list[str]`` with no readers and
    no spelling: the 39 shipped manifests between them used 34 distinct
    strings for 22 capabilities (``screen`` / ``screen_capture`` /
    ``computer.screen``; ``shell`` / ``execution`` / ``execute`` /
    ``process_spawn``). A free-text field cannot be rendered in an
    install dialog, because the dialog has to say what the string means
    and there is no answer for a string nobody has seen before.

    Closed on purpose. A manifest naming a permission that is not a
    member fails to load, so a third party cannot smuggle a capability
    past the consent dialog by inventing a word for it. Adding a member
    here is the deliberate act of teaching the UI a new sentence, and
    ``PERMISSION_DESCRIPTIONS`` below is where that sentence is written.
    """

    FILESYSTEM = "filesystem"
    NETWORK = "network"
    CODE_EXECUTION = "code_execution"
    SCREEN = "screen"
    INPUT_CONTROL = "input_control"
    CAMERA = "camera"
    VISION = "vision"
    MESSAGING = "messaging"
    CONTACTS = "contacts"
    CALENDAR = "calendar"
    HEALTH_DATA = "health_data"
    SMART_HOME = "smart_home"
    HARDWARE = "hardware"
    SYSTEM_SETTINGS = "system_settings"
    MEMORY = "memory"
    IDENTITY = "identity"
    NOTIFICATIONS = "notifications"
    SCHEDULING = "scheduling"
    AUTONOMY = "autonomy"
    LLM = "llm"
    BROWSER = "browser"
    COMMERCE = "commerce"

    # Format as the wire value, not "SkillPermission.FILESYSTEM". A
    # permission id ends up in log lines and error strings, and the
    # default Enum.__str__ would put a Python identifier in front of a
    # user. json.dumps already emits the value (str subclass); this makes
    # f-strings agree with it.
    __str__ = str.__str__


# Short label + one plain sentence per permission. This is consent copy,
# not developer documentation: it is rendered verbatim in the install
# dialog, so it says what the skill can reach in words someone who has
# never read this file can act on. The API ships these strings with the
# permission list rather than letting each client invent its own, so
# there is exactly one place the wording can be reviewed.
PERMISSION_LABELS: dict[SkillPermission, str] = {
    SkillPermission.FILESYSTEM: "Your files",
    SkillPermission.NETWORK: "Internet access",
    SkillPermission.CODE_EXECUTION: "Run programs",
    SkillPermission.SCREEN: "Screen contents",
    SkillPermission.INPUT_CONTROL: "Keyboard and mouse",
    SkillPermission.CAMERA: "Camera",
    SkillPermission.VISION: "Image analysis",
    SkillPermission.MESSAGING: "Messages",
    SkillPermission.CONTACTS: "Contacts",
    SkillPermission.CALENDAR: "Calendar",
    SkillPermission.HEALTH_DATA: "Health data",
    SkillPermission.SMART_HOME: "Smart home devices",
    SkillPermission.HARDWARE: "Connected hardware",
    SkillPermission.SYSTEM_SETTINGS: "System settings",
    SkillPermission.MEMORY: "Stored memory",
    SkillPermission.IDENTITY: "Your profile",
    SkillPermission.NOTIFICATIONS: "Notifications",
    SkillPermission.SCHEDULING: "Background schedules",
    SkillPermission.AUTONOMY: "Acting on its own",
    SkillPermission.LLM: "Model providers",
    SkillPermission.BROWSER: "Your web browser",
    SkillPermission.COMMERCE: "Purchases",
}

PERMISSION_DESCRIPTIONS: dict[SkillPermission, str] = {
    SkillPermission.FILESYSTEM:
        "Read and write files on this computer, including documents "
        "outside FERAL's own folder.",
    SkillPermission.NETWORK:
        "Contact servers on the internet and send them data it can reach.",
    SkillPermission.CODE_EXECUTION:
        "Run shell commands and other programs on this computer under "
        "your user account.",
    SkillPermission.SCREEN:
        "Capture what is on your screen, including any window you have open.",
    SkillPermission.INPUT_CONTROL:
        "Move the pointer, type, and click in any app as if it were you.",
    SkillPermission.CAMERA:
        "Turn on a camera and take photos or video.",
    SkillPermission.VISION:
        "Send images, such as screenshots or camera frames, to a model "
        "to be described.",
    SkillPermission.MESSAGING:
        "Read and send messages in the messaging accounts you connected.",
    SkillPermission.CONTACTS:
        "Read your contact list.",
    SkillPermission.CALENDAR:
        "Read your calendar and create or change events.",
    SkillPermission.HEALTH_DATA:
        "Read health and fitness records synced from your devices.",
    SkillPermission.SMART_HOME:
        "See and control smart home devices such as lights, locks, and "
        "thermostats.",
    SkillPermission.HARDWARE:
        "Send commands to robots and other hardware paired with FERAL.",
    SkillPermission.SYSTEM_SETTINGS:
        "Read and change system settings such as volume, display, and "
        "network.",
    SkillPermission.MEMORY:
        "Read and write what FERAL has stored locally about you.",
    SkillPermission.IDENTITY:
        "Read your name, accounts, and the other identity details FERAL holds.",
    SkillPermission.NOTIFICATIONS:
        "Send you notifications.",
    SkillPermission.SCHEDULING:
        "Schedule work and run it later on its own, without you starting it.",
    SkillPermission.AUTONOMY:
        "Start multi-step actions and other agents without asking you for "
        "each step.",
    SkillPermission.LLM:
        "Send your data to a language model provider to be processed.",
    SkillPermission.BROWSER:
        "Drive your web browser: open pages, click, and fill in forms.",
    SkillPermission.COMMERCE:
        "Place orders and make payments on your behalf.",
}


def permission_values(permissions) -> list[str]:
    """Wire values for a manifest's permission list.

    ``SkillManifest.permissions`` holds enum members after validation,
    but pydantic does not validate plain attribute assignment, so a
    caller that sets the list by hand can leave raw strings in it. This
    accepts either rather than raising inside a listing endpoint.
    """
    return [str(getattr(p, "value", p)) for p in (permissions or [])]


def describe_permissions(permissions) -> list[dict]:
    """Render a permission list as ``[{id, label, description}]``.

    Used by the marketplace preview / install / catalog responses so the
    consent copy is served from one place instead of being reinvented in
    each client.
    """
    out: list[dict] = []
    for raw in permissions or []:
        try:
            perm = SkillPermission(raw)
        except ValueError:
            # Unreachable through SkillManifest (the field is validated),
            # but describe_permissions is also called on registry payloads
            # that have not been through the model yet. Name the unknown
            # value rather than dropping it: a permission we cannot
            # explain is exactly what the user needs to see.
            value = str(raw)
            out.append({
                "id": value,
                "label": value,
                "description": "Unrecognised permission. FERAL cannot say what this reaches.",
                "known": False,
            })
            continue
        out.append({
            "id": perm.value,
            "label": PERMISSION_LABELS[perm],
            "description": PERMISSION_DESCRIPTIONS[perm],
            "known": True,
        })
    return out


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

    # Opt in to sending a human-readable excerpt of this endpoint's result to
    # the chat UI. Default off: results routinely carry vault reads, tokens,
    # file contents and mail bodies, and there is no redaction pass in this
    # codebase. Same trust clamp as result_budget - honoured only for
    # manifests shipping in feral-core/skills/manifests/.
    emit_result_preview: bool = False


class FlowStep(BaseModel):
    """One step in a multi-step flow."""
    endpoint_id: str
    condition: Optional[str] = None  # e.g. "result.rain_probability > 0.5"
    then_endpoint_id: Optional[str] = None


class SkillFlow(BaseModel):
    """A multi-step orchestration (e.g., search → select → order → confirm).

    Two readers, and it is worth knowing which is which:

    * ``SkillRegistry._flow_to_taskflow_steps`` translates a flow into
      TaskFlow ``skill.invoke`` steps, reachable only from a
      ``CronDefinition`` that names this flow's id in ``flow_id``. That
      is the only path that EXECUTES a flow.
    * ``self_introspection.describe_skill`` reports the flow to the
      model as a recipe, so a flow with no cron is still useful: the
      model can follow the sequence itself, one gated tool call at a
      time.

    ``FlowStep.condition`` / ``then_endpoint_id`` are read by neither.
    Branching is not translated (see the docstring on
    ``_flow_to_taskflow_steps``), so a flow that relies on it will run
    its steps unconditionally on the cron path.
    """
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
    """An event-driven reaction.

    ``condition`` is evaluated by
    ``ProactiveEngine._evaluate_manifest_triggers``, which then NOTIFIES
    and stops.

    ``action_flow_id`` and ``action_endpoint_id`` are named in the
    notification and are deliberately NOT dispatched. That is not an
    oversight to be tidied up later. Before the evaluator existed,
    ``skills/registry.py`` turned each trigger into a routine polling
    every minute with the condition parked in a payload nobody read, so
    the action ran unconditionally: 4,766 runs each on two routines,
    one of them a Telegram send gated on a stress reading that was
    never checked. Wiring the action back up is a change that has to go
    through the cron/surface safety pre-flight in ``api/server.py``, not
    a field default.

    A declared id must still name a flow or endpoint that exists in the
    same manifest; ``tests/test_manifest_declarations_have_readers.py``
    fails the build otherwise, because a field nothing dispatches is
    the easiest one in the schema to get wrong unnoticed.
    """
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
    
    # Permissions. Closed vocabulary (see SkillPermission): this list is
    # what the install dialog shows the user before anything is written
    # to disk, so every member has to be a capability the UI can name.
    permissions: list[SkillPermission] = []
    
    # Hardware requirements (for hardware skills)
    requires_daemon: bool = False
    daemon_node_type: Optional[str] = None  # "desktop", "rpi", "robot"
    # If True, every endpoint in this skill must execute inside a sandbox.
    # Runtime enforcement lives in `skills/executor.py`.
    requires_sandbox: bool = False
    
    # Rate limits. Enforced by ``SkillExecutor._rate_limit`` at the same
    # dispatch chokepoint as the plan-mode and approval gates: a rolling
    # one-hour window, counted per skill, refusing with status 429 and a
    # retry-after rather than calling the vendor. 0 or less means
    # unlimited. It was declared in 39 of the 41 shipped manifests, with
    # tuned per-skill values, and read by nothing at all until the
    # limiter landed.
    max_calls_per_hour: int = 1000

    # Default result budget tier for every endpoint in this skill; see
    # SkillEndpoint.result_budget and skills/result_budget.py.
    result_budget: Optional[str] = None

    # Opt in to sending a human-readable excerpt of this endpoint's result to
    # the chat UI. Default off: results routinely carry vault reads, tokens,
    # file contents and mail bodies, and there is no redaction pass in this
    # codebase. Same trust clamp as result_budget - honoured only for
    # manifests shipping in feral-core/skills/manifests/.
    emit_result_preview: bool = False


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
