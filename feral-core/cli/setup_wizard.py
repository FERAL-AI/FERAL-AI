"""
FERAL Interactive Onboarding
==============================
Guided setup that configures everything: LLM provider, identity,
agent personality, tool keys, device pairing, and more.

Steps:
  1. Welcome + FERAL mission
  2. LLM provider + API key (validated)
  3. Model selection
  4. Tell the agent about YOU (USER.md) — expanded identity
  5. Agent personality / SOUL.md
  6. Device pairing (optional)
  7. Tool API keys (search, weather, image gen, GitHub, etc.)
  8. Summary + how to start

All config is saved to ~/.feral/ — no env vars needed.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import socket
import textwrap
from pathlib import Path

from config.loader import feral_home
from config.runtime import ollama_base_url

logger = logging.getLogger("feral.cli.setup_wizard")


# ─────────────────────────────────────────────────────────────────────
# Credential persistence — routed through the  encrypted BlindVault.
#
# A7 cleanup (2026-04-26): these two helpers replaced the direct
# plaintext ``credentials.json`` writers that used to live in
# ``OnboardWizard._save_all`` and ``OnboardWizardPlain.run``. The
# encrypted vault maps ``credentials.json`` → ``credentials.enc``
# internally, so anchoring it at the wizard home keeps the on-disk
# artefact inside ``~/.feral/`` without leaving a cleartext file.
#
# Returning users with a  plaintext ``credentials.json`` still
# get their keys preserved: instantiating ``BlindVault`` triggers the
# built-in auto-migration (``credentials.json`` → ``credentials.enc``
# with the original moved to ``credentials.json.bak.legacy``).
# ─────────────────────────────────────────────────────────────────────


def _load_vault_creds(home: Path) -> dict | None:
    """Return credentials from the encrypted vault, or ``None`` if the
    vault is unavailable (so callers can decide whether to fall back
    to a legacy plaintext read)."""
    try:
        from security.vault import BlindVault
    except Exception as exc:  # pragma: no cover — import-time failure
        logger.warning("setup_wizard: vault import failed (%s)", exc)
        return None
    try:
        vault = BlindVault(vault_path=str(home / "credentials.json"))
    except Exception as exc:
        logger.warning("setup_wizard: vault init failed (%s)", exc)
        return None
    try:
        return {k: vault.retrieve(k) or "" for k in vault.list_keys()}
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("setup_wizard: vault read failed (%s)", exc)
        return None


def _persist_vault_creds(home: Path, credentials: dict) -> None:
    """Write every credential to the encrypted vault.

    Empty values are skipped. When the vault is unavailable we refuse
    to fall back to plaintext — the credentials stay in memory for the
    current process and the user is instructed to fix the vault on
    next boot via ``feral key recover``.
    """
    flat = {k: v for k, v in credentials.items() if isinstance(v, str) and v}
    if not flat:
        return
    try:
        from security.vault import BlindVault
    except Exception as exc:  # pragma: no cover — import-time failure
        logger.error(
            "setup_wizard: vault import failed (%s); refusing to "
            "persist plaintext credentials. Keys remain in memory only.",
            exc,
        )
        return
    try:
        vault = BlindVault(vault_path=str(home / "credentials.json"))
    except Exception as exc:
        logger.error(
            "setup_wizard: vault init failed (%s); refusing to persist "
            "plaintext credentials. Keys remain in memory only.",
            exc,
        )
        return
    for key, value in flat.items():
        try:
            vault.set_credential(key, value)
        except Exception as exc:
            logger.error("setup_wizard: vault write for %s failed: %s", key, exc)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt, Confirm
    from rich.progress import Progress, SpinnerColumn, TextColumn
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

FERAL_HOME = feral_home()


# ─────────────────────────────────────────────────────────
# Prompt helpers — always accept Enter as the default value.
#
# Rich's Prompt.ask with ``choices=`` rejects empty/whitespace input even
# when a ``default`` is supplied, which led to the infamous 11× "Please
# select one of the available options" loop reported by users. These
# helpers normalise the input and retry with the default so pressing
# Enter always picks the default.
# ─────────────────────────────────────────────────────────


def _ask_choice(prompt: str, choices, default: str) -> str:
    """Ask a multiple-choice question where Enter → ``default``.

    Extra leniency: numeric input ("2") picks the Nth choice, and the
    caller may pass choices as a list of strings (shown verbatim).
    """
    if not HAS_RICH:
        while True:
            raw = input(f"{prompt} [{default}]: ").strip()
            if not raw:
                return default
            if raw in choices:
                return raw
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(choices):
                    return choices[idx]
            print(f"  Pick one of: {', '.join(choices)} (or press Enter for '{default}')")

    while True:
        try:
            raw = Prompt.ask(prompt, default=default, show_default=True)
        except (EOFError, KeyboardInterrupt):
            raise
        raw = (raw or "").strip()
        if not raw:
            return default
        if raw in choices:
            return raw
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        Console().print(
            f"  [yellow]Pick one of:[/] {', '.join(choices)} "
            f"[dim](press Enter for '{default}')[/]"
        )


def _ask_text(prompt: str, default: str = "") -> str:
    """Ask for free-form text where Enter → ``default``."""
    if not HAS_RICH:
        raw = input(f"{prompt}{' [' + default + ']' if default else ''}: ").strip()
        return raw or default
    try:
        raw = Prompt.ask(prompt, default=default, show_default=bool(default))
    except (EOFError, KeyboardInterrupt):
        raise
    raw = (raw or "").strip()
    return raw or default

# ═══════════════════════════════════════════════════════════
# LLM Providers — wizard now reads model lists from the catalog
# ═══════════════════════════════════════════════════════════
#
# Until 2026-04 this dict carried hardcoded ``models`` and
# ``default_model`` literals (e.g. ``gpt-4.1``, ``claude-sonnet-4-20250514``,
# ``gemini-2.5-flash``). Those names go stale every quarter and shipped
# to production unnoticed (Roadmap §3.5 P0). We now hold only the
# user-facing chrome (display name, env var, hint text) here and ask
# ``providers.catalog`` for the live model list at runtime via
# ``provider_models()`` / ``provider_default_model()`` below.

PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "env_key": "OPENAI_API_KEY",
        "base_url": "",
        "desc": "GPT-5.x, realtime voice, multimodal",
        "models": [],
        "default_model": "",
        "voice": True,
        "key_hint": "Starts with sk-...",
    },
    "anthropic": {
        "name": "Anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "base_url": "",
        "desc": "Claude Opus / Sonnet / Haiku, strong reasoning",
        "models": [],
        "default_model": "",
        "voice": False,
        "key_hint": "Starts with sk-ant-...",
    },
    "gemini": {
        "name": "Google Gemini",
        "env_key": "GOOGLE_API_KEY",
        "base_url": "",
        "desc": "Gemini 3.x preview lineup, realtime voice, multimodal",
        "models": [],
        "default_model": "",
        "voice": True,
        "key_hint": "From Google AI Studio",
    },
    "openrouter": {
        "name": "OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "desc": "Gateway to 300+ models (OpenAI, Claude, Gemini, DeepSeek, Llama)",
        "models": [],
        "default_model": "",
        "voice": False,
        "key_hint": "From openrouter.ai/keys",
    },
    "deepseek": {
        "name": "DeepSeek",
        "env_key": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "desc": "DeepSeek V3 / R1, strong reasoning, low cost",
        "models": [],
        "default_model": "",
        "voice": False,
        "key_hint": "From platform.deepseek.com",
    },
    "kimi": {
        "name": "Kimi (Moonshot)",
        "env_key": "MOONSHOT_API_KEY",
        # API host is api.moonshot.ai. The DOCS host moved to
        # platform.kimi.ai but the API host did NOT.
        "base_url": "https://api.moonshot.ai/v1",
        "desc": "Kimi K3 / K2.7, 1M context, strong Chinese + English",
        "models": [],
        "default_model": "",
        "voice": False,
        "key_hint": "From platform.kimi.ai",
    },
    "qwen": {
        "name": "Qwen (Alibaba)",
        "env_key": "DASHSCOPE_API_KEY",
        # Workspace-scoped since 2026; {WorkspaceId} is substituted
        # from DASHSCOPE_WORKSPACE_ID at connect time.
        "base_url": "https://[{WorkspaceId}].ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        "desc": "Qwen3.7 Max/Plus, Qwen3.6 Flash, strong multilingual",
        "models": [],
        "default_model": "",
        "voice": False,
        "key_hint": "From dashscope.console.aliyun.com",
    },
    "groq": {
        "name": "Groq",
        "env_key": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "desc": "Ultra-fast inference, Llama 3.3 / DeepSeek / Mixtral",
        "models": [],
        "default_model": "",
        "voice": False,
        "key_hint": "From console.groq.com",
    },
    "ollama": {
        "name": "Ollama (Local)",
        "env_key": "",
        "base_url": "",
        "desc": "Free, private, runs on your machine — no API key needed",
        "models": [],
        "default_model": "",
        "voice": False,
        "key_hint": "No key needed — just install Ollama",
    },
    "lmstudio": {
        "name": "LM Studio (Local)",
        "env_key": "",
        "base_url": "http://localhost:1234/v1",
        "desc": "Free, private, GUI model manager — no API key needed",
        "models": [],
        "default_model": "",
        "voice": False,
        "key_hint": "No key needed — just launch LM Studio and load a model",
    },
}


# ── Catalog-backed helpers ─────────────────────────────────
#
# Catalog id ↔ wizard id (only entries that diverge). The wizard
# historically used ``kimi``; the catalog speaks ``moonshot``.
_WIZARD_TO_CATALOG = {"kimi": "moonshot"}


def provider_models(provider_id: str) -> list[str]:
    """Return the catalog's bundled / live model list for *provider_id*.

    Falls back to an empty list when the catalog is unavailable
    (e.g. running the wizard before the brain has booted). Callers
    should treat ``[]`` as "type any model id you want".
    """
    pid = _WIZARD_TO_CATALOG.get(provider_id, provider_id)
    try:
        from providers.catalog import get_shared_catalog
        catalog = get_shared_catalog()
        adapter = catalog.get_adapter(pid)
        if adapter is not None:
            return list(adapter.list_models() or [])
    except Exception:
        pass
    return []


def provider_default_model(provider_id: str) -> str:
    """Return the current default model id from the catalog.

    Empty string means "no default" — let the user type a value.
    """
    pid = _WIZARD_TO_CATALOG.get(provider_id, provider_id)
    try:
        from providers.catalog import get_shared_catalog
        return get_shared_catalog().default_model_for(pid) or ""
    except Exception:
        return ""

# ═══════════════════════════════════════════════════════════
# Tool API Keys
# ═══════════════════════════════════════════════════════════

TOOL_KEYS = [
    {
        "env": "EXA_API_KEY",
        "name": "EXA Search",
        "desc": "Neural search (best quality, understands meaning)",
        "hint": "From exa.ai/dashboard — free tier available",
        "optional": True,
    },
    {
        "env": "TAVILY_API_KEY",
        "name": "Tavily",
        "desc": "Web search (fast, structured results)",
        "hint": "From tavily.com — free tier available",
        "optional": True,
    },
    {
        "env": "SERPER_API_KEY",
        "name": "Serper",
        "desc": "Google search results API",
        "hint": "From serper.dev — 2500 free queries",
        "optional": True,
    },
    {
        "env": "BRAVE_API_KEY",
        "name": "Brave Search",
        "desc": "Web search (privacy-focused alternative)",
        "hint": "From brave.com/search/api",
        "optional": True,
    },
    {
        "env": "OPENWEATHER_API_KEY",
        "name": "OpenWeatherMap",
        "desc": "Weather data and forecasts",
        "hint": "From openweathermap.org — free tier",
        "optional": True,
    },
    {
        "env": "GITHUB_TOKEN",
        "name": "GitHub",
        "desc": "Repo operations, issues, PRs",
        "hint": "From github.com/settings/tokens (classic or fine-grained)",
        "optional": True,
    },
    {
        "env": "SPOTIFY_CLIENT_ID",
        "name": "Spotify",
        "desc": "Music playback and playlist control",
        "hint": "From developer.spotify.com — also needs SPOTIFY_CLIENT_SECRET",
        "optional": True,
        "extra_keys": ["SPOTIFY_CLIENT_SECRET"],
    },
    {
        "env": "GOOGLE_CALENDAR_CREDENTIALS",
        "name": "Google Calendar",
        "desc": "Scheduling, event management",
        "hint": "From console.cloud.google.com — OAuth JSON path",
        "optional": True,
    },
]

# ═══════════════════════════════════════════════════════════
# Messaging Channels
# ═══════════════════════════════════════════════════════════

CHANNELS = [
    {
        "id": "telegram",
        "name": "Telegram Bot",
        "fields": [("FERAL_TELEGRAM_BOT_TOKEN", "Bot token from @BotFather")],
        "validate_url": lambda fv: f"https://api.telegram.org/bot{fv['FERAL_TELEGRAM_BOT_TOKEN']}/getMe",
    },
    {
        "id": "slack",
        "name": "Slack",
        "fields": [
            ("FERAL_SLACK_BOT_TOKEN", "Bot token (xoxb-...)"),
            ("FERAL_SLACK_APP_TOKEN", "App-level token (xapp-...) — optional for Socket Mode"),
        ],
        "validate_url": "https://slack.com/api/auth.test",
        "validate_headers": lambda fv: {"Authorization": f"Bearer {fv['FERAL_SLACK_BOT_TOKEN']}"},
    },
    {
        "id": "discord",
        "name": "Discord Bot",
        "fields": [("FERAL_DISCORD_BOT_TOKEN", "Bot token from Discord Developer Portal")],
        "validate_url": "https://discord.com/api/v10/users/@me",
        "validate_headers": lambda fv: {"Authorization": f"Bot {fv['FERAL_DISCORD_BOT_TOKEN']}"},
    },
    {
        "id": "whatsapp",
        "name": "WhatsApp Cloud API",
        "fields": [
            ("WHATSAPP_PHONE_NUMBER_ID", "Phone number ID from Meta Business"),
            ("WHATSAPP_ACCESS_TOKEN", "Permanent access token"),
            ("WHATSAPP_VERIFY_TOKEN", "Webhook verify token (you pick)"),
        ],
        "validate_url": lambda fv: f"https://graph.facebook.com/v18.0/{fv['WHATSAPP_PHONE_NUMBER_ID']}",
        "validate_headers": lambda fv: {"Authorization": f"Bearer {fv['WHATSAPP_ACCESS_TOKEN']}"},
    },
]

# ═══════════════════════════════════════════════════════════
# Shared Validation Helpers
# ═══════════════════════════════════════════════════════════


async def validate_provider_key(provider: str, key: str) -> tuple[bool, str]:
    """Validate an LLM provider API key by hitting its models endpoint.
    Returns (success, message).
    """
    try:
        import httpx
    except ImportError:
        return True, "httpx not installed, skipping validation"

    endpoints: dict[str, tuple[str, dict]] = {
        "openai": ("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}),
        "anthropic": ("https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"}),
        "gemini": (f"https://generativelanguage.googleapis.com/v1/models?key={key}", {}),
        "groq": ("https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {key}"}),
        "openrouter": ("https://openrouter.ai/api/v1/models", {"Authorization": f"Bearer {key}"}),
        "deepseek": ("https://api.deepseek.com/models", {"Authorization": f"Bearer {key}"}),
        "kimi": ("https://api.moonshot.ai/v1/models", {"Authorization": f"Bearer {key}"}),
        # Qwen key validation uses the non-workspace-scoped international
        # DashScope host: the workspace-scoped chat host needs a
        # workspace id the operator has not supplied yet at this point
        # in the wizard.
        "qwen": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models", {"Authorization": f"Bearer {key}"}),
    }

    if provider not in endpoints:
        return True, "No validation available for this provider"

    url, headers = endpoints[provider]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                return True, "Key is valid!"
            return False, f"HTTP {r.status_code} — key may be invalid or expired"
    except Exception as e:
        return False, f"Connection error: {e}"


async def _validate_channel(channel: dict, field_values: dict[str, str]) -> tuple[bool, str]:
    """Validate channel credentials by calling the provider's API."""
    try:
        import httpx
    except ImportError:
        return True, "httpx not installed, skipping validation"

    validate_url = channel.get("validate_url")
    if not validate_url:
        return True, "No validation endpoint"

    url = validate_url(field_values) if callable(validate_url) else validate_url
    headers: dict[str, str] = {}
    hdr_fn = channel.get("validate_headers")
    if hdr_fn and callable(hdr_fn):
        headers = hdr_fn(field_values)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                return True, f"{channel['name']} connected!"
            return False, f"Validation failed (HTTP {r.status_code})"
    except Exception as e:
        return False, f"Connection error: {e}"


async def _validate_home_assistant(url: str, token: str) -> tuple[bool, str]:
    """Validate Home Assistant by hitting GET {url}/api/."""
    try:
        import httpx
    except ImportError:
        return True, "httpx not installed, skipping validation"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{url.rstrip('/')}/api/",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("message") == "API running.":
                    return True, "Home Assistant connected!"
                return True, "Home Assistant responded"
            return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"Connection error: {e}"


# ═══════════════════════════════════════════════════════════
# Personality Presets
# ═══════════════════════════════════════════════════════════

PERSONALITY_PRESETS = {
    "assistant": {
        "label": "Personal Assistant",
        "desc": "Warm, direct, and efficient. Remembers your preferences.",
        "soul": textwrap.dedent("""\
            You are a warm, capable personal assistant. You speak naturally, like
            a trusted colleague who knows the user well. You're direct — no filler,
            no over-explaining — but never cold. You proactively notice patterns in
            the user's data and mention things that might be useful. You remember
            past conversations and learn preferences over time. When you don't know
            something, you say so honestly.
        """),
    },
    "engineer": {
        "label": "Technical Partner",
        "desc": "Precise, analytical, code-first. Minimal small talk.",
        "soul": textwrap.dedent("""\
            You are a senior engineer partner. You think in systems, prefer data
            over opinion, and communicate with precision. When asked a question,
            you give the answer first, then context. You use technical language
            naturally but explain when asked. You suggest better approaches when
            you see them. You don't sugarcoat — if something is wrong, you say it.
        """),
    },
    "coach": {
        "label": "Wellness Coach",
        "desc": "Encouraging, health-focused, motivational.",
        "soul": textwrap.dedent("""\
            You are a supportive health and wellness coach. You celebrate progress,
            no matter how small. You interpret health data with context and empathy —
            not just numbers. You encourage consistency over perfection. You know when
            to push and when to back off. You make health data approachable and
            actionable instead of overwhelming.
        """),
    },
    "minimal": {
        "label": "Minimal",
        "desc": "Extremely concise. Short answers. Zero filler.",
        "soul": textwrap.dedent("""\
            You are extremely concise. One sentence when possible. No greetings,
            no filler, no "certainly" or "of course". Just the answer. Use bullet
            points for lists. Numbers without prose. You only elaborate when the
            user explicitly asks for more detail.
        """),
    },
    "custom": {
        "label": "Custom",
        "desc": "Write your own personality from scratch.",
        "soul": "",
    },
}


def _looks_like_vision_model(model_name: str) -> bool:
    lower = (model_name or "").lower()
    return any(token in lower for token in ("llava", "moondream", "qwen2-vl", "minicpm-v", "bakllava", "gemma3"))


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "YOUR_IP"


def run_setup():
    """Entry point — delegates to the new modular wizard.

    Kept as a thin shim so any external script that imports
    ``cli.setup_wizard.run_setup`` continues to work; the modular
    implementation lives in ``cli.setup``.
    """
    from cli.setup import run_setup as _run_setup

    _run_setup()


# ═══════════════════════════════════════════════════════════
# Rich-powered wizard
# ═══════════════════════════════════════════════════════════

class OnboardWizard:

    def __init__(self, console: Console):
        self.c = console
        self.config: dict = {}
        self.creds: dict = {}

    async def run(self):
        FERAL_HOME.mkdir(parents=True, exist_ok=True)
        self._load_existing_creds()

        self._step_welcome()
        await self._step_provider()
        await self._step_model()
        await self._step_about_you()
        await self._step_personality()
        await self._step_device_pairing()
        await self._step_tool_keys()
        await self._step_channels()
        await self._step_home_assistant()
        self._step_api_key()
        self._save_all()
        self._step_finish()

    def _load_existing_creds(self):
        """Load previously-saved credentials.

        Priority: encrypted vault () → legacy plaintext
        ``credentials.json`` ( installs that have not yet booted
        the brain since migration). The vault auto-migrates the
        plaintext file on first instantiation, so once the wizard has
        ``save``d at least once there is nothing plaintext left on
        disk.
        """
        creds_path = FERAL_HOME / "credentials.json"
        loaded = _load_vault_creds(FERAL_HOME)
        if loaded is not None:
            self.creds = loaded
            return
        if creds_path.exists():
            try:
                self.creds = json.loads(creds_path.read_text())
            except Exception:
                self.creds = {}

    # ── Welcome + Mission ─────────────────────────────────

    def _step_welcome(self):
        self.c.print()
        self.c.print(Panel.fit(
            "[bold cyan]Welcome to FERAL[/]\n"
            "[bold]The Open AI Operating System[/]\n\n"
            "[dim]FERAL is not just another computer-use agent.[/]\n\n"
            "Unlike tools that only control your screen,\n"
            "FERAL is a [bold]full platform[/]:\n\n"
            "  [cyan]•[/] Learns new skills on the fly — the agent teaches itself\n"
            "  [cyan]•[/] Controls hardware — glasses, robots, sensors, home devices\n"
            "  [cyan]•[/] Generates dynamic UI — no hardcoded apps, just data\n"
            "  [cyan]•[/] Privacy-first memory — your data stays on YOUR machine\n"
            "  [cyan]•[/] Multi-device — phone as bridge to glasses, wristbands, robots\n\n"
            "[dim]Built for AI developers to extend: add skills, hardware daemons,\n"
            "GenUI providers, and more. Our vision is a native AI OS built on NixOS\n"
            "that runs on PCs, phones, and embedded devices.[/]\n\n"
            "[bold green]This wizard sets everything up in about 3 minutes.[/]",
            border_style="cyan",
            padding=(1, 2),
        ))
        self.c.print()

    # ── Step 1: Provider ────────────────────────────────────

    async def _step_provider(self):
        self.c.print(Panel(
            "[bold]Step 1 · LLM Provider[/]\n"
            "[dim]Which AI model provider do you want to use?[/]",
            style="blue",
        ))

        table = Table(show_header=True, header_style="bold")
        table.add_column("#", style="dim", width=3)
        table.add_column("Provider", style="cyan", width=20)
        table.add_column("Description", width=50)
        table.add_column("Voice", width=6)
        table.add_column("Status", width=16)

        provider_keys = list(PROVIDERS.keys())
        for i, pid in enumerate(provider_keys, 1):
            p = PROVIDERS[pid]
            env_key = p["env_key"]
            has_key = bool(self.creds.get(env_key) or os.getenv(env_key, "")) if env_key else False
            status = "[green]configured[/]" if has_key else "[dim]not set[/]"
            if pid == "ollama":
                status = "[dim]local[/]"
            voice = "[green]yes[/]" if p["voice"] else "[dim]no[/]"
            table.add_row(str(i), p["name"], p["desc"], voice, status)

        self.c.print(table)
        self.c.print()

        choice = Prompt.ask(
            "Choose provider",
            choices=provider_keys,
            default="openai",
        )

        provider = PROVIDERS[choice]
        self.config["provider"] = choice

        if provider.get("base_url"):
            self.config["base_url"] = provider["base_url"]

        if choice == "ollama":
            await self._check_ollama()
        elif choice == "lmstudio":
            await self._check_lmstudio()
        elif provider["env_key"]:
            existing = self.creds.get(provider["env_key"]) or os.getenv(provider["env_key"], "")
            if existing:
                masked = existing[:8] + "..." + existing[-4:] if len(existing) > 12 else "***"
                self.c.print(f"  Existing key found: {masked}")
                if Confirm.ask("  Use this key?", default=True):
                    api_key = existing
                else:
                    api_key = Prompt.ask(f"  Enter {provider['name']} API key", password=True)
            else:
                self.c.print(f"  [dim]{provider['key_hint']}[/]")
                api_key = Prompt.ask(f"  Enter {provider['name']} API key", password=True)

            if api_key:
                with Progress(SpinnerColumn(), TextColumn("{task.description}")) as prog:
                    prog.add_task("Validating key...", total=None)
                    valid = await self._validate_key(choice, api_key)

                if valid:
                    self.c.print("  [green]Key is valid![/]")
                else:
                    self.c.print("  [yellow]Key could not be validated — saving anyway. Check it with `feral doctor`.[/]")

                self.creds[provider["env_key"]] = api_key
                os.environ[provider["env_key"]] = api_key

        self.c.print()

    # ── Step 2: Model ───────────────────────────────────────

    async def _step_model(self):
        provider_id = self.config.get("provider", "openai")
        provider = PROVIDERS[provider_id]

        if provider_id == "lmstudio":
            models = await self._list_lmstudio_models()
            if models:
                self.c.print(Panel("[bold]Step 2 · Model[/]", style="blue"))
                for i, m in enumerate(models, 1):
                    self.c.print(f"  {i}. {m}")
                model = Prompt.ask("Choose model", default=models[0])
                self.config["model"] = model
            else:
                self.c.print("  [dim]No LM Studio models loaded. Open LM Studio and load a model.[/]")
                self.config["model"] = "local-model"
            self.c.print()
            return

        if provider_id == "ollama":
            models = await self._list_ollama_models()
            if models:
                self.c.print(Panel("[bold]Step 2 · Model[/]", style="blue"))
                for i, m in enumerate(models, 1):
                    self.c.print(f"  {i}. {m}")
                model = Prompt.ask("Choose model", default=models[0] if models else "llama3.1")
                self.config["model"] = model
                self.config["local_preset"] = "ollama_vision" if _looks_like_vision_model(model) else "ollama_text"

                vision_models = [m for m in models if _looks_like_vision_model(m)]
                if vision_models and Confirm.ask("Enable Ollama local vision path now?", default=True):
                    vlm_model = Prompt.ask("Vision model", default=vision_models[0])
                    self.config["vlm_provider"] = "ollama"
                    self.config["vlm_model"] = vlm_model
                    self.config["local_preset"] = "ollama_vision"
                elif not vision_models and Confirm.ask(
                    "No vision model found. Pull llava:7b for local vision? (~4.7 GB)",
                    default=False,
                ):
                    await self._auto_pull_vision()
            else:
                self.c.print("  [dim]No Ollama models found. Pull one: ollama pull llama3.1[/]")
                self.config["model"] = "llama3.1"
                self.config["local_preset"] = "ollama_text"
            self.c.print()
            return

        provider_id = self.config.get("provider", "")
        models = provider_models(provider_id)
        default_model = provider_default_model(provider_id) or (models[0] if models else "")
        if not models:
            self.c.print(Panel(
                "[bold]Step 2 · Model[/]\n"
                f"[dim]The catalog has no cached {provider['name']} models. Enter the provider model id.[/]",
                style="blue",
            ))
            model = Prompt.ask("Enter model id", default=default_model or None)
            if model:
                self.config["model"] = model
            self.c.print()
            return

        self.c.print(Panel(
            "[bold]Step 2 · Model[/]\n"
            f"[dim]Which {provider['name']} model?[/]",
            style="blue",
        ))

        for i, m in enumerate(models, 1):
            default_marker = " [green](recommended)[/]" if m == default_model else ""
            self.c.print(f"  {i}. {m}{default_marker}")

        model = Prompt.ask(
            "Choose model",
            choices=models,
            default=default_model,
        )
        self.config["model"] = model
        self.c.print()

    # ── Step 3: About YOU (expanded) ──────────────────────

    async def _step_about_you(self):
        self.c.print(Panel(
            "[bold]Step 3 · About You[/]\n"
            "[dim]Tell your agent about yourself so it can be more helpful.\n"
            "This is saved locally in ~/.feral/USER.md — only your agent reads it.[/]",
            style="blue",
        ))

        user_path = FERAL_HOME / "USER.md"
        existing = ""
        if user_path.exists():
            existing = user_path.read_text().strip()
            if existing and existing != _DEFAULT_USER_MD.strip():
                self.c.print(f"  [dim]Existing USER.md found ({len(existing)} chars)[/]")
                if not Confirm.ask("  Overwrite it?", default=False):
                    self.c.print("  [green]Keeping existing USER.md[/]\n")
                    return

        name = Prompt.ask("  Your name", default="")
        location = Prompt.ask("  Where do you live (city/country)", default="")
        language = Prompt.ask("  Preferred language", default="English")
        occupation = Prompt.ask("  What do you do (job, student, etc)", default="")
        interests = Prompt.ask("  Interests / hobbies (comma-separated)", default="")

        self.c.print()
        self.c.print("  [dim]These help your agent match your style:[/]")
        tech_levels = ["beginner", "intermediate", "advanced", "developer"]
        tech_level = _ask_choice(
            "  Tech skill level",
            tech_levels,
            default="intermediate",
        )

        use_cases = ["personal-assistant", "developer-tool", "health-monitoring", "home-automation", "research", "other"]
        use_case = _ask_choice(
            "  Primary use case",
            use_cases,
            default="personal-assistant",
        )

        comm_styles = ["detailed", "concise", "casual", "formal"]
        comm_style = _ask_choice(
            "  Communication preference",
            comm_styles,
            default="concise",
        )

        # Optional FERAL community section
        self.c.print()
        is_feral = Confirm.ask("  Are you part of the FERAL ecosystem? (glasses/wristband)", default=False)
        health_goals = ""
        glasses_model = ""
        wristband = False
        if is_feral:
            health_goals = Prompt.ask("  Health goals or conditions to track", default="")
            glasses_model = Prompt.ask("  FERAL glasses model (W300/W610/other/none)", default="none")
            wristband = Confirm.ask("  FERAL wristband connected?", default=False)

        anything_else = Prompt.ask("  Anything else your agent should know", default="")

        lines = ["# About Me\n"]
        if name:
            lines.append(f"My name is {name}.")
        if location:
            lines.append(f"I live in {location}.")
        if language and language.lower() != "english":
            lines.append(f"I prefer to communicate in {language}.")
        if occupation:
            lines.append(f"I work as {occupation}." if "student" not in occupation.lower() else f"I'm a {occupation}.")

        lines.append("\n## Preferences")
        lines.append(f"- Tech level: {tech_level}")
        lines.append(f"- Primary use: {use_case}")
        lines.append(f"- Communication style: {comm_style}")

        if interests:
            lines.append(f"\n## Interests\n{interests}")

        if is_feral:
            lines.append("\n## FERAL Ecosystem")
            if health_goals:
                lines.append(f"- Health goals: {health_goals}")
            if glasses_model and glasses_model.lower() != "none":
                lines.append(f"- Glasses model: {glasses_model}")
            if wristband:
                lines.append("- Wristband: connected")

        if anything_else:
            lines.append(f"\n## Notes\n{anything_else}")

        user_md = "\n".join(lines) + "\n"
        user_path.write_text(user_md)
        self.c.print(f"  [green]Saved to {user_path}[/]")
        self.c.print("  [dim]You can edit this file anytime to update your agent's knowledge about you.[/]")
        self.c.print()

    # ── Step 4: Agent Personality ───────────────────────────

    async def _step_personality(self):
        self.c.print(Panel(
            "[bold]Step 4 · Agent Personality[/]\n"
            "[dim]How should your agent behave? This defines its SOUL.md.[/]",
            style="blue",
        ))

        preset_keys = list(PERSONALITY_PRESETS.keys())
        for i, key in enumerate(preset_keys, 1):
            p = PERSONALITY_PRESETS[key]
            self.c.print(f"  {i}. [cyan]{p['label']}[/] [dim]({key})[/] — {p['desc']}")

        def _resolve_personality(raw: str) -> str | None:
            """Map '1', 'assistant', 'personal assistant' → a key in PERSONALITY_PRESETS."""
            v = (raw or "").strip().lower()
            if not v:
                return "assistant"
            if v.isdigit():
                idx = int(v) - 1
                if 0 <= idx < len(preset_keys):
                    return preset_keys[idx]
                return None
            if v in PERSONALITY_PRESETS:
                return v
            for k, p in PERSONALITY_PRESETS.items():
                if v == p["label"].strip().lower():
                    return k
                if v in p["label"].strip().lower():
                    return k
            return None

        choice = None
        while choice is None:
            try:
                raw = Prompt.ask("Choose personality", default="1")
            except (EOFError, KeyboardInterrupt):
                raise
            choice = _resolve_personality(raw)
            if choice is None:
                self.c.print(
                    f"  [yellow]Pick a number 1–{len(preset_keys)} or one of:[/] "
                    f"{', '.join(preset_keys)}"
                )

        preset = PERSONALITY_PRESETS[choice]

        if choice == "custom":
            self.c.print("  Write your agent's personality (multi-line, press Enter twice to finish):")
            lines = []
            while True:
                line = Prompt.ask("  ", default="")
                if line == "" and lines and lines[-1] == "":
                    break
                lines.append(line)
            soul_text = "\n".join(lines).strip()
        else:
            soul_text = preset["soul"].strip()
            self.c.print(f"\n  [dim]{soul_text[:120]}...[/]\n")

        agent_name = Prompt.ask("  Agent name", default="FERAL")
        self.config["agent_name"] = agent_name

        soul_path = FERAL_HOME / "SOUL.md"
        soul_content = f"# {agent_name}\n\n{soul_text}\n"
        soul_path.write_text(soul_content)
        self.c.print("  [green]SOUL.md saved[/]")

        try:
            import yaml
        except ImportError:
            yaml = None

        identity = {
            "name": agent_name,
            "tagline": f"{agent_name} — your personal AI operating system",
            "personality": soul_text,
            "rules": [
                "Never share user data without explicit consent",
                "Explain before taking impactful actions",
                "Be honest about limitations",
                "Keep responses concise unless asked for detail",
                "Respect privacy — everything runs locally unless explicitly told otherwise",
            ],
            "greeting_style": "Brief and contextual. If you have recent data, mention something relevant.",
            "voice": {"tts_voice": "nova", "speed": 1.0},
        }

        identity_path = FERAL_HOME / "IDENTITY.yaml"
        if yaml:
            identity_path.write_text(yaml.dump(identity, default_flow_style=False, allow_unicode=True, sort_keys=False))
        else:
            identity_path.write_text(json.dumps(identity, indent=2))
        self.c.print("  [green]IDENTITY.yaml saved[/]")

        memory_path = FERAL_HOME / "MEMORY.md"
        if not memory_path.exists():
            memory_path.write_text("# Agent Memory\n\nLong-term curated memory. The agent updates this file as it learns.\n")
            self.c.print("  [green]MEMORY.md created[/]")

        self.config["multi_agent"] = Confirm.ask(
            "  Enable Multi-Agent mode (subagents + parallel workers)?",
            default=True,
        )
        mode_label = "enabled" if self.config["multi_agent"] else "disabled"
        self.c.print(f"  [green]Multi-Agent {mode_label}[/]")
        self.c.print()

    # ── Step 5: Device Pairing ─────────────────────────────

    async def _step_device_pairing(self):
        self.c.print(Panel(
            "[bold]Step 5 · Connect Your Devices (Optional)[/]\n"
            "[dim]FERAL can connect to your phone, glasses, wristband, and more.\n"
            "Your phone acts as a bridge between wearables and this computer.[/]",
            style="blue",
        ))

        self.c.print(
            "  [bold]How it works:[/]\n"
            "  ┌─────────────┐     ┌───────────┐     ┌────────────┐     ┌──────────┐\n"
            "  │  Glasses /  │────▶│   Phone   │────▶│   Brain    │────▶│  Actions │\n"
            "  │  Wristband  │     │  (Bridge) │     │ (This Mac) │     │ (Robot,  │\n"
            "  │  Sensors    │     │           │     │            │     │  Apps..) │\n"
            "  └─────────────┘     └───────────┘     └────────────┘     └──────────┘\n"
        )

        local_ip = _get_local_ip()
        self.c.print(f"  [dim]Your local IP: {local_ip}[/]")
        self.c.print(f"  [dim]Daemon WebSocket: ws://{local_ip}:9090/v1/daemon[/]")
        self.c.print()

        pair_phone = Confirm.ask("  Pair a phone as a bridge now?", default=False)
        if pair_phone:
            self.c.print(
                "\n  [dim]The FERAL Node app on your phone prints its bridge URL in\n"
                "  Settings → Connection → Copy URL. It looks like:[/]\n"
                f"    [cyan]ws://{local_ip}:9091/bridge[/]  [dim](your phone's IP + the bridge port)[/]\n"
                "  [dim]Press Enter to skip — FERAL will auto-discover it over mDNS when\n"
                "  you start the brain.[/]\n"
            )
            phone_url = Prompt.ask(
                "  Phone bridge URL (Enter = auto-discover via mDNS)",
                default="",
            )
            phone_url = (phone_url or "").strip()
            if phone_url:
                self.config["phone_bridge_url"] = phone_url
                self.c.print(f"  [green]Phone bridge URL saved:[/] {phone_url}")
            else:
                self.c.print(
                    "  [dim]Auto-discovery will search for `_feral-phone._tcp.local.` on\n"
                    "  your LAN at boot. Make sure the phone app is running on the same Wi-Fi.[/]"
                )
                self.config["phone_bridge_url"] = "auto"

        register_glasses = Confirm.ask("  Register FERAL glasses?", default=False)
        if register_glasses:
            model = Prompt.ask("  Glasses model (W300/W610/other)", default="W610")
            self.config["glasses_model"] = model
            self.c.print(f"  [green]Registered: {model}[/]")

        if not pair_phone and not register_glasses:
            self.c.print("  [dim]Skipped. You can pair devices later: feral devices pair[/]")

        self.c.print()

    # ── Step 6: Tool API Keys ──────────────────────────────

    async def _step_tool_keys(self):
        self.c.print(Panel(
            "[bold]Step 6 · Tool API Keys (Optional)[/]\n"
            "[dim]These unlock extra capabilities. Skip any you don't need — \n"
            "FERAL works without them (falls back to free alternatives).\n"
            "Your LLM key (if OpenAI) already enables DALL-E image generation.[/]",
            style="blue",
        ))

        for tk in TOOL_KEYS:
            existing = self.creds.get(tk["env"]) or os.getenv(tk["env"], "")
            if existing:
                self.c.print(f"  [green]{tk['name']}[/]: configured")
                continue

            if Confirm.ask(f"  Add {tk['name']}? ({tk['desc']})", default=False):
                self.c.print(f"    [dim]{tk['hint']}[/]")
                key = Prompt.ask(f"    {tk['name']} API key", password=True)
                if key.strip():
                    self.creds[tk["env"]] = key.strip()
                    if "extra_keys" in tk:
                        for ek in tk["extra_keys"]:
                            extra_val = Prompt.ask(f"    {ek}", password=True)
                            if extra_val.strip():
                                self.creds[ek] = extra_val.strip()
                    self.c.print("    [green]Saved[/]")
            else:
                self.c.print("    [dim]Skipped[/]")

        self.c.print()

    # ── Step 7: Messaging Channels ───────────────────────

    async def _step_channels(self):
        self.c.print(Panel(
            "[bold]Step 7 · Messaging Channels (Optional)[/]\n"
            "[dim]FERAL can talk on Telegram, Slack, Discord, and WhatsApp.[/]",
            style="blue",
        ))

        if not Confirm.ask("  Connect messaging channels?", default=False):
            self.c.print("  [dim]Skipped. Configure later: feral setup[/]")
            self.c.print()
            return

        configured: list[str] = []
        for ch in CHANNELS:
            if not Confirm.ask(f"  Configure {ch['name']}?", default=False):
                continue

            while True:
                field_values: dict[str, str] = {}
                for env_key, hint in ch["fields"]:
                    existing = self.creds.get(env_key)
                    if existing:
                        masked = existing[:6] + "..." if len(existing) > 8 else "***"
                        self.c.print(f"    {env_key}: {masked} [green](existing)[/]")
                        if Confirm.ask("    Use existing?", default=True):
                            field_values[env_key] = existing
                            continue
                    self.c.print(f"    [dim]{hint}[/]")
                    is_secret = any(kw in env_key.lower() for kw in ("token", "secret", "key", "password"))
                    val = Prompt.ask(f"    {env_key}", password=is_secret)
                    if val.strip():
                        field_values[env_key] = val.strip()

                if not field_values:
                    break

                with Progress(SpinnerColumn(), TextColumn("{task.description}")) as prog:
                    prog.add_task(f"Validating {ch['name']}...", total=None)
                    ok, msg = await _validate_channel(ch, field_values)

                if ok:
                    self.c.print(f"    [green]{msg}[/]")
                    self.creds.update(field_values)
                    configured.append(ch["name"])
                    break
                else:
                    self.c.print(f"    [yellow]{msg}[/]")
                    action = Prompt.ask("    Retry / Save anyway / Skip",
                                        choices=["retry", "save", "skip"], default="retry")
                    if action == "save":
                        self.creds.update(field_values)
                        configured.append(ch["name"])
                        break
                    elif action == "skip":
                        break

        self.config["channels"] = configured
        self.c.print()

    # ── Step 8: Home Assistant ───────────────────────────

    async def _step_home_assistant(self):
        self.c.print(Panel(
            "[bold]Step 8 · Home Assistant (Optional)[/]\n"
            "[dim]Connect FERAL to your Home Assistant for smart home control.[/]",
            style="blue",
        ))

        if not Confirm.ask("  Do you use Home Assistant?", default=False):
            self.c.print("  [dim]Skipped[/]")
            self.c.print()
            return

        while True:
            ha_url = Prompt.ask("  Home Assistant URL", default="http://homeassistant.local:8123")
            ha_token = Prompt.ask("  Long-lived access token", password=True)

            if not ha_url or not ha_token:
                break

            with Progress(SpinnerColumn(), TextColumn("{task.description}")) as prog:
                prog.add_task("Validating Home Assistant...", total=None)
                ok, msg = await _validate_home_assistant(ha_url, ha_token)

            if ok:
                self.c.print(f"  [green]{msg}[/]")
                self.creds["HA_URL"] = ha_url
                self.creds["HA_TOKEN"] = ha_token
                self.config["home_assistant"] = True
                break
            else:
                self.c.print(f"  [yellow]{msg}[/]")
                action = Prompt.ask("  Retry / Save anyway / Skip",
                                    choices=["retry", "save", "skip"], default="retry")
                if action == "save":
                    self.creds["HA_URL"] = ha_url
                    self.creds["HA_TOKEN"] = ha_token
                    self.config["home_assistant"] = True
                    break
                elif action == "skip":
                    break

        self.c.print()

    # ── FERAL API Key ─────────────────────────────────────

    def _step_api_key(self):
        from api.keys import load_or_generate_api_key, get_api_key_path

        load_or_generate_api_key()
        key_path = get_api_key_path()
        self.c.print(Panel(
            f"[bold]FERAL API Key[/]\n\n"
            f"  Stored at: [cyan]{key_path}[/]\n"
            f"  [dim]Clients (iOS, Android, browser) use this to authenticate.\n"
            f"  Set FERAL_API_KEY env var to override.[/]",
            style="green",
        ))
        self.c.print()

    # ── Save & Finish ──────────────────────────────────────

    def _save_all(self):
        _persist_vault_creds(FERAL_HOME, self.creds)

        config_path = FERAL_HOME / "config.json"
        config_path.write_text(json.dumps(self.config, indent=2))

        provider_id = self.config.get("provider", "openai")
        provider_info = PROVIDERS.get(provider_id, {})

        settings = {
            "llm": {
                "provider": provider_id,
                "model": self.config.get("model", "") or provider_default_model(provider_id),
                "base_url": self.config.get("base_url", provider_info.get("base_url", "")),
            },
            "vision": {
                "enabled": bool(self.config.get("vlm_provider")),
                "provider": self.config.get("vlm_provider", ""),
                "model": self.config.get("vlm_model", ""),
            },
            "devices": {
                "phone_bridge_url": self.config.get("phone_bridge_url", ""),
                "glasses_model": self.config.get("glasses_model", ""),
            },
            "features": {
                "multi_agent": bool(self.config.get("multi_agent", True)),
            },
            "channels": {
                "configured": self.config.get("channels", []),
                **{
                    ch: {"enabled": True}
                    for ch in self.config.get("channels", [])
                    if ch in ("telegram", "discord", "slack", "whatsapp")
                },
            },
            "home_assistant": {
                "enabled": bool(self.config.get("home_assistant")),
            },
            "meta": {
                "local_preset": self.config.get("local_preset", ""),
                "setup_complete": True,
            },
        }
        settings_path = FERAL_HOME / "settings.json"
        existing = {}
        if settings_path.exists():
            try:
                existing = json.loads(settings_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        for section, values in settings.items():
            if section not in existing:
                existing[section] = {}
            if isinstance(values, dict) and isinstance(existing[section], dict):
                existing[section].update(values)
            else:
                existing[section] = values

        settings_path.write_text(json.dumps(existing, indent=2))

    def _step_finish(self):
        provider_name = PROVIDERS.get(self.config.get("provider", ""), {}).get("name", "?")
        model = self.config.get("model", "default")
        agent_name = self.config.get("agent_name", "FERAL")
        tool_count = sum(1 for tk in TOOL_KEYS if tk["env"] in self.creds)
        channels = self.config.get("channels", [])
        channel_str = ", ".join(channels) if channels else "none"
        ha_status = "connected" if self.config.get("home_assistant") else "not configured"

        self.c.print(Panel.fit(
            f"[bold green]FERAL Setup Complete[/]\n\n"
            f"  LLM Provider:    {provider_name}\n"
            f"  LLM Model:       {model}\n"
            f"  Agent:           {agent_name}\n"
            f"  Tool Keys:       {tool_count} configured\n"
            f"  Channels:        {channel_str}\n"
            f"  Home Assistant:  {ha_status}\n"
            f"  FERAL API Key:   ~/.feral/api_key [dim](keep this safe)[/]\n\n"
            f"[bold]Starting FERAL on http://localhost:9090 ...[/]",
            border_style="green",
            padding=(1, 2),
        ))

    # ── Helpers ─────────────────────────────────────────────

    async def _auto_pull_vision(self):
        """Use auto_setup_vision to pull a vision model for Ollama."""
        from agents.local_inference import auto_setup_vision

        with Progress(SpinnerColumn(), TextColumn("{task.description}")) as prog:
            prog.add_task("Pulling llava:7b — this may take a few minutes…", total=None)
            result = await auto_setup_vision()

        if result.get("available"):
            model = result.get("model", "llava:7b")
            self.config["vlm_provider"] = "ollama"
            self.config["vlm_model"] = model
            self.config["local_preset"] = "ollama_vision"
            pulled_tag = " (just pulled)" if result.get("pulled") else ""
            self.c.print(f"  [green]Vision model ready: {model}{pulled_tag}[/]")
        else:
            self.c.print("  [yellow]Could not pull vision model. You can pull manually: ollama pull llava:7b[/]")

    async def _validate_key(self, provider: str, key: str) -> bool:
        """Backward-compat wrapper around the module-level validate_provider_key."""
        ok, _ = await validate_provider_key(provider, key)
        return ok

    async def _check_ollama(self):
        try:
            import httpx
            base = ollama_base_url()
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{base}/api/tags")
                if r.status_code == 200:
                    models = [m["name"] for m in r.json().get("models", [])]
                    if models:
                        self.c.print(f"  [green]Ollama running with {len(models)} model(s)[/]")
                    else:
                        self.c.print("  [yellow]Ollama running but no models. Pull one: ollama pull llama3.1[/]")
                    return
        except Exception:
            pass
        self.c.print("  [yellow]Ollama not running. Start it: ollama serve[/]")

    async def _list_ollama_models(self) -> list[str]:
        try:
            import httpx
            base = ollama_base_url()
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{base}/api/tags")
                if r.status_code == 200:
                    return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            pass
        return []

    async def _check_lmstudio(self):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get("http://localhost:1234/v1/models")
                if r.status_code == 200:
                    models = r.json().get("data", [])
                    if models:
                        self.c.print(f"  [green]LM Studio running with {len(models)} model(s)[/]")
                    else:
                        self.c.print("  [yellow]LM Studio running but no model loaded. Load one in the GUI.[/]")
                    return
        except Exception:
            pass
        self.c.print("  [yellow]LM Studio not running. Launch it and load a model.[/]")

    async def _list_lmstudio_models(self) -> list[str]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get("http://localhost:1234/v1/models")
                if r.status_code == 200:
                    return [m.get("id", "unknown") for m in r.json().get("data", [])]
        except Exception:
            pass
        return []


# ═══════════════════════════════════════════════════════════
# Plain fallback (no rich)
# ═══════════════════════════════════════════════════════════

class OnboardWizardPlain:

    def __init__(self):
        self.config: dict = {}
        self.creds: dict = {}

    async def run(self):
        FERAL_HOME.mkdir(parents=True, exist_ok=True)

        loaded = _load_vault_creds(FERAL_HOME)
        if loaded is not None:
            self.creds = loaded
        else:
            creds_path = FERAL_HOME / "credentials.json"
            if creds_path.exists():
                try:
                    self.creds = json.loads(creds_path.read_text())
                except Exception:
                    self.creds = {}

        print()
        print("=" * 60)
        print("  FERAL — The Open AI Operating System")
        print()
        print("  Not just another computer-use agent. FERAL is a full")
        print("  platform: learns skills, controls hardware, generates UI,")
        print("  privacy-first memory. Built for AI devs to extend.")
        print()
        print("  (install 'rich' for a better experience: pip install rich)")
        print("=" * 60)
        print()

        # Provider
        print("Step 1: LLM Provider")
        provider_keys = list(PROVIDERS.keys())
        for i, pid in enumerate(provider_keys, 1):
            p = PROVIDERS[pid]
            print(f"  {i}. {p['name']:20s} {p['desc']}")
        choice_idx = input(f"  Choose (1-{len(provider_keys)}) [1]: ").strip() or "1"
        try:
            provider_id = provider_keys[int(choice_idx) - 1]
        except (ValueError, IndexError):
            provider_id = "openai"
        provider = PROVIDERS[provider_id]
        self.config["provider"] = provider_id

        if provider.get("base_url"):
            self.config["base_url"] = provider["base_url"]

        if provider["env_key"]:
            existing = self.creds.get(provider["env_key"]) or os.getenv(provider["env_key"], "")
            if existing:
                print(f"  Key found: {existing[:8]}...")
                use = input("  Use this key? (Y/n): ").strip().lower()
                if use not in ("n", "no"):
                    api_key = existing
                else:
                    api_key = input(f"  Enter {provider['name']} API key: ").strip()
            else:
                api_key = input(f"  Enter {provider['name']} API key: ").strip()
            if api_key:
                while True:
                    print("  Validating key...")
                    ok, msg = await validate_provider_key(provider_id, api_key)
                    if ok:
                        print(f"  {msg}")
                        self.creds[provider["env_key"]] = api_key
                        os.environ[provider["env_key"]] = api_key
                        break
                    else:
                        print(f"  {msg}")
                        retry = input("  Retry with different key? (y/N): ").strip().lower()
                        if retry in ("y", "yes"):
                            api_key = input(f"  Enter {provider['name']} API key: ").strip()
                            if not api_key:
                                break
                        else:
                            print("  Saving anyway.")
                            self.creds[provider["env_key"]] = api_key
                            os.environ[provider["env_key"]] = api_key
                            break
        print()

        # Model
        if provider_id == "lmstudio":
            print("Step 2: Model")
            lms_models = await self._list_lmstudio_models()
            if lms_models:
                for i, m in enumerate(lms_models, 1):
                    print(f"  {i}. {m}")
                model_input = input(f"  Choose [{lms_models[0]}]: ").strip()
                self.config["model"] = model_input or lms_models[0]
            else:
                print("  No LM Studio models loaded. Open LM Studio and load a model.")
                self.config["model"] = "local-model"
        elif provider_id == "ollama":
            print("Step 2: Model")
            ollama_models = await self._list_ollama_models()
            if ollama_models:
                for i, m in enumerate(ollama_models, 1):
                    print(f"  {i}. {m}")
                model_input = input(f"  Choose [{ollama_models[0]}]: ").strip()
                self.config["model"] = model_input or ollama_models[0]
                vision_models = [m for m in ollama_models if _looks_like_vision_model(m)]
                if vision_models:
                    use_vision = input(f"  Enable local vision with {vision_models[0]}? (Y/n): ").strip().lower()
                    if use_vision not in ("n", "no"):
                        self.config["vlm_provider"] = "ollama"
                        self.config["vlm_model"] = vision_models[0]
                        self.config["local_preset"] = "ollama_vision"
                else:
                    pull_vision = input("  No vision model found. Pull llava:7b (~4.7 GB)? (y/N): ").strip().lower()
                    if pull_vision in ("y", "yes"):
                        print("  Pulling llava:7b — this may take a few minutes…")
                        from agents.local_inference import auto_setup_vision
                        result = await auto_setup_vision()
                        if result.get("available"):
                            model_name = result.get("model", "llava:7b")
                            self.config["vlm_provider"] = "ollama"
                            self.config["vlm_model"] = model_name
                            self.config["local_preset"] = "ollama_vision"
                            print(f"  Vision model ready: {model_name}")
                        else:
                            print("  Could not pull vision model. Try manually: ollama pull llava:7b")
            else:
                print("  No Ollama models found. Pull one: ollama pull llama3.1")
                self.config["model"] = "llama3.1"
        else:
            provider_id = self.config.get("provider", "")
            models = provider_models(provider_id)
            default_model = provider_default_model(provider_id) or (models[0] if models else "")
            if models:
                print("Step 2: Model")
                for i, m in enumerate(models, 1):
                    default_marker = " (default)" if m == default_model else ""
                    print(f"  {i}. {m}{default_marker}")
                model_input = input(f"  Choose [{default_model}]: ").strip()
                self.config["model"] = model_input if model_input in models else default_model
            elif default_model:
                self.config["model"] = default_model
        print()

        # About you (expanded)
        print("Step 3: About You")
        print("  Tell your agent about yourself (saved to ~/.feral/USER.md)")
        name = input("  Your name: ").strip()
        location = input("  City/Country: ").strip()
        language = input("  Preferred language [English]: ").strip() or "English"
        occupation = input("  Occupation: ").strip()
        interests = input("  Interests: ").strip()
        print("  Tech level: 1. beginner  2. intermediate  3. advanced  4. developer")
        tech_idx = input("  Choose [2]: ").strip() or "2"
        tech_map = {"1": "beginner", "2": "intermediate", "3": "advanced", "4": "developer"}
        tech_level = tech_map.get(tech_idx, "intermediate")
        print("  Use case: 1. personal-assistant  2. developer-tool  3. health-monitoring")
        print("            4. home-automation  5. research  6. other")
        use_idx = input("  Choose [1]: ").strip() or "1"
        use_map = {"1": "personal-assistant", "2": "developer-tool", "3": "health-monitoring",
                   "4": "home-automation", "5": "research", "6": "other"}
        use_case = use_map.get(use_idx, "personal-assistant")
        print("  Communication: 1. detailed  2. concise  3. casual  4. formal")
        comm_idx = input("  Choose [2]: ").strip() or "2"
        comm_map = {"1": "detailed", "2": "concise", "3": "casual", "4": "formal"}
        comm_style = comm_map.get(comm_idx, "concise")

        feral_member = input("  Part of FERAL ecosystem (glasses/wristband)? (y/N): ").strip().lower()
        health_goals = ""
        glasses_model = ""
        if feral_member in ("y", "yes"):
            health_goals = input("  Health goals: ").strip()
            glasses_model = input("  Glasses model (W300/W610/other/none): ").strip()

        lines = ["# About Me\n"]
        if name:
            lines.append(f"My name is {name}.")
        if location:
            lines.append(f"I live in {location}.")
        if language.lower() != "english":
            lines.append(f"I prefer to communicate in {language}.")
        if occupation:
            lines.append(f"I work as {occupation}.")
        lines.append("\n## Preferences")
        lines.append(f"- Tech level: {tech_level}")
        lines.append(f"- Primary use: {use_case}")
        lines.append(f"- Communication style: {comm_style}")
        if interests:
            lines.append(f"\n## Interests\n{interests}")
        if feral_member in ("y", "yes"):
            lines.append("\n## FERAL Ecosystem")
            if health_goals:
                lines.append(f"- Health goals: {health_goals}")
            if glasses_model and glasses_model.lower() != "none":
                lines.append(f"- Glasses model: {glasses_model}")
        (FERAL_HOME / "USER.md").write_text("\n".join(lines) + "\n")
        print("  Saved.")
        print()

        # Personality
        print("Step 4: Agent Personality")
        print("  1. Personal Assistant   2. Technical Partner")
        print("  3. Wellness Coach       4. Minimal")
        preset_map = {"1": "assistant", "2": "engineer", "3": "coach", "4": "minimal"}
        p_choice = input("  Choose (1-4) [1]: ").strip() or "1"
        preset_id = preset_map.get(p_choice, "assistant")
        soul = PERSONALITY_PRESETS[preset_id]["soul"]

        agent_name = input("  Agent name [FERAL]: ").strip() or "FERAL"
        self.config["agent_name"] = agent_name

        (FERAL_HOME / "SOUL.md").write_text(f"# {agent_name}\n\n{soul}\n")

        identity = {"name": agent_name, "personality": soul}
        try:
            import yaml
            (FERAL_HOME / "IDENTITY.yaml").write_text(
                yaml.dump(identity, default_flow_style=False, sort_keys=False)
            )
        except ImportError:
            (FERAL_HOME / "IDENTITY.yaml").write_text(json.dumps(identity, indent=2))
        multi_agent_choice = input("  Enable Multi-Agent mode? (Y/n): ").strip().lower()
        self.config["multi_agent"] = multi_agent_choice not in ("n", "no")
        print("  Saved.")
        print()

        # Device pairing
        print("Step 5: Device Pairing (optional)")
        print("  Architecture: Glasses/Sensors -> Phone (Bridge) -> Brain (This PC) -> Actions")
        local_ip = _get_local_ip()
        print(f"  Your local IP: {local_ip}")
        print(f"  Daemon WebSocket: ws://{local_ip}:9090/v1/daemon")
        pair = input("  Pair a phone bridge now? (y/N): ").strip().lower()
        if pair in ("y", "yes"):
            print(f"    Example: ws://{local_ip}:9091/bridge  (from the FERAL Node app on your phone)")
            print("    Press Enter to skip and auto-discover via mDNS at startup.")
            url = input("  Phone bridge URL: ").strip()
            self.config["phone_bridge_url"] = url or "auto"
        glasses = input("  Register FERAL glasses? (y/N): ").strip().lower()
        if glasses in ("y", "yes"):
            model = input("  Glasses model (W300/W610/other) [W610]: ").strip() or "W610"
            self.config["glasses_model"] = model
        print()

        # Tool keys
        print("Step 6: Extra API Keys (optional, press Enter to skip)")
        for tk in TOOL_KEYS:
            existing = self.creds.get(tk["env"]) or os.getenv(tk["env"], "")
            if existing:
                print(f"  {tk['name']}: configured")
                continue
            key = input(f"  {tk['name']} ({tk['desc']}): ").strip()
            if key:
                self.creds[tk["env"]] = key
                if "extra_keys" in tk:
                    for ek in tk["extra_keys"]:
                        extra_val = input(f"  {ek}: ").strip()
                        if extra_val:
                            self.creds[ek] = extra_val
        print()

        # Channels
        print("Step 7: Messaging Channels (optional)")
        print("  FERAL can talk on Telegram, Slack, Discord, and WhatsApp.")
        configure_channels = input("  Connect messaging channels? (y/N): ").strip().lower()
        configured_channels: list[str] = []
        if configure_channels in ("y", "yes"):
            for ch in CHANNELS:
                setup_ch = input(f"  Configure {ch['name']}? (y/N): ").strip().lower()
                if setup_ch not in ("y", "yes"):
                    continue
                while True:
                    field_values: dict[str, str] = {}
                    for env_key, hint in ch["fields"]:
                        existing_val = self.creds.get(env_key)
                        if existing_val:
                            print(f"    {env_key}: {existing_val[:6]}... (existing)")
                            use_ex = input("    Use existing? (Y/n): ").strip().lower()
                            if use_ex not in ("n", "no"):
                                field_values[env_key] = existing_val
                                continue
                        print(f"    {hint}")
                        val = input(f"    {env_key}: ").strip()
                        if val:
                            field_values[env_key] = val
                    if not field_values:
                        break
                    print(f"  Validating {ch['name']}...")
                    ch_ok, ch_msg = await _validate_channel(ch, field_values)
                    if ch_ok:
                        print(f"    {ch_msg}")
                        self.creds.update(field_values)
                        configured_channels.append(ch["name"])
                        break
                    else:
                        print(f"    {ch_msg}")
                        action = input("    [r]etry / [s]ave anyway / s[k]ip: ").strip().lower()
                        if action in ("s", "save"):
                            self.creds.update(field_values)
                            configured_channels.append(ch["name"])
                            break
                        elif action in ("k", "skip"):
                            break
        self.config["channels"] = configured_channels
        print()

        # Home Assistant
        print("Step 8: Home Assistant (optional)")
        ha_setup = input("  Do you use Home Assistant? (y/N): ").strip().lower()
        if ha_setup in ("y", "yes"):
            while True:
                ha_url = input("  Home Assistant URL [http://homeassistant.local:8123]: ").strip()
                ha_url = ha_url or "http://homeassistant.local:8123"
                ha_token = input("  Long-lived access token: ").strip()
                if not ha_url or not ha_token:
                    break
                print("  Validating Home Assistant...")
                ha_ok, ha_msg = await _validate_home_assistant(ha_url, ha_token)
                if ha_ok:
                    print(f"  {ha_msg}")
                    self.creds["HA_URL"] = ha_url
                    self.creds["HA_TOKEN"] = ha_token
                    self.config["home_assistant"] = True
                    break
                else:
                    print(f"  {ha_msg}")
                    ha_action = input("  [r]etry / [s]ave anyway / s[k]ip: ").strip().lower()
                    if ha_action in ("s", "save"):
                        self.creds["HA_URL"] = ha_url
                        self.creds["HA_TOKEN"] = ha_token
                        self.config["home_assistant"] = True
                        break
                    elif ha_action in ("k", "skip"):
                        break
        print()

        # FERAL API Key
        from api.keys import load_or_generate_api_key as _gen_key, get_api_key_path as _key_path
        _gen_key()
        _kp = _key_path()
        print(f"  FERAL API Key stored at: {_kp}")
        print("  Clients (iOS, Android, browser) use this to authenticate.")
        print()

        # Vision settings for plain wizard
        if self.config.get("vlm_provider"):
            settings_vision = {
                "enabled": True,
                "provider": self.config["vlm_provider"],
                "model": self.config.get("vlm_model", ""),
            }
        else:
            settings_vision = {"enabled": False, "provider": "", "model": ""}

        _persist_vault_creds(FERAL_HOME, self.creds)
        (FERAL_HOME / "config.json").write_text(json.dumps(self.config, indent=2))

        provider_id = self.config.get("provider", "openai")
        provider_info = PROVIDERS.get(provider_id, {})
        settings = {
            "llm": {
                "provider": provider_id,
                "model": self.config.get("model", "") or provider_default_model(provider_id),
                "base_url": self.config.get("base_url", provider_info.get("base_url", "")),
            },
            "vision": settings_vision,
            "devices": {
                "phone_bridge_url": self.config.get("phone_bridge_url", ""),
                "glasses_model": self.config.get("glasses_model", ""),
            },
            "features": {
                "multi_agent": bool(self.config.get("multi_agent", True)),
            },
            "channels": {
                "configured": self.config.get("channels", []),
                **{
                    ch: {"enabled": True}
                    for ch in self.config.get("channels", [])
                    if ch in ("telegram", "discord", "slack", "whatsapp")
                },
            },
            "home_assistant": {
                "enabled": bool(self.config.get("home_assistant")),
            },
            "meta": {
                "local_preset": self.config.get("local_preset", ""),
                "setup_complete": True,
            },
        }
        settings_path = FERAL_HOME / "settings.json"
        existing_settings = {}
        if settings_path.exists():
            try:
                existing_settings = json.loads(settings_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        for section, values in settings.items():
            if section not in existing_settings:
                existing_settings[section] = {}
            if isinstance(values, dict) and isinstance(existing_settings[section], dict):
                existing_settings[section].update(values)
            else:
                existing_settings[section] = values

        settings_path.write_text(json.dumps(existing_settings, indent=2))

        tool_count = sum(1 for tk in TOOL_KEYS if tk["env"] in self.creds)
        channels_list = self.config.get("channels", [])
        channel_str = ", ".join(channels_list) if channels_list else "none"
        ha_status = "connected" if self.config.get("home_assistant") else "not configured"

        print("=" * 55)
        print("  FERAL Setup Complete")
        print("=" * 55)
        print(f"  LLM Provider:    {provider_info.get('name', '?')}")
        print(f"  LLM Model:       {self.config.get('model', 'default')}")
        print(f"  Agent:           {agent_name}")
        print(f"  Tool Keys:       {tool_count} configured")
        print(f"  Channels:        {channel_str}")
        print(f"  Home Assistant:  {ha_status}")
        print("  FERAL API Key:   ~/.feral/api_key (keep this safe)")
        print()
        print("  Starting FERAL on http://localhost:9090 ...")
        print("=" * 55)
        print()


    async def _list_ollama_models(self) -> list[str]:
        try:
            import httpx
            base = ollama_base_url()
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{base}/api/tags")
                if r.status_code == 200:
                    return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            pass
        return []

    async def _list_lmstudio_models(self) -> list[str]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get("http://localhost:1234/v1/models")
                if r.status_code == 200:
                    return [m.get("id", "unknown") for m in r.json().get("data", [])]
        except Exception:
            pass
        return []


_DEFAULT_USER_MD = "# About Me\n\nTell your agent about yourself here.\n"
