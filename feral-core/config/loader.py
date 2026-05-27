"""
FERAL Config Loader — Layered Configuration System
=====================================================
Inspired by claw-code-parity's ConfigLoader: merges settings from
multiple sources in priority order.

Hierarchy (highest priority wins):
  1. Environment variables (FERAL_*)
  2. Local config  (.feral/settings.local.json) — machine-specific, gitignored
  3. Project config (.feral/settings.json) — shared with team
  4. User config    (~/.feral/settings.json) — user-global defaults

Credentials are stored separately in the encrypted BlindVault
(``~/.feral/credentials.enc``) and NEVER merged into settings.

Skills are discovered from:
  - ~/.feral/skills/           (user-installed)
  - .feral/skills/             (project-local)
  - Built-in manifests in feral-core/skills/manifests/
"""

from __future__ import annotations
import copy
import json
import logging
import os
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger("feral.config")

DEFAULT_SETTINGS = {
    "version": "0.4.0",
    "llm": {
        "provider": "openai",
        "model": "gpt-4o-mini",
        # Lane U1 — multi-model favorites. ``llm.model`` stays the
        # active scalar choice (back-compat for every caller that
        # reads it). ``llm.models`` is the additive favorites list
        # that ``feral models add`` appends to and ``feral models
        # set`` keeps deduped. Empty default keeps the existing
        # contract: callers that only know about ``llm.model``
        # continue to work unchanged.
        "models": [],
        "base_url": "",
        "fallback_providers": [],
        # Optional spend controls for failover routing. Zero budget keeps
        # historical provider-priority behaviour.
        "daily_budget_usd": 0.0,
        "daily_spend_usd": 0.0,
        "budget_tight_ratio": 0.25,
    },
    "audio": {
        "stt_provider": "openai",
        "stt_model": "whisper-1",
        "tts_provider": "openai",
        "tts_model": "tts-1",
        "tts_voice": "nova",
        # Ordered list of voice realtime providers the router tries
        # before flipping to chunked TTS. ``openai`` resolves to
        # ``RealtimeProxy`` (OpenAI Realtime API), ``gemini`` to
        # ``GeminiRealtimeProxy``. Override per install via
        # ``~/.feral/settings.json``.
        "realtime_providers": ["openai", "gemini"],
        # OpenAI Realtime model id passed to the RealtimeProxy when
        # opening a session. Operator-overridable via
        # ``feral setup`` (voice preflight) or the WebUI Voice card;
        # the runtime falls back to ``RealtimeProxy.DEFAULT_MODEL``
        # when this key is unset (Lane U2).
        "realtime_model": "gpt-realtime",
        # Ordered list of fallback TTS providers consulted when the
        # primary realtime provider fails (OpenAI 1013
        # insufficient_quota, 429, invalid_api_key). The router walks
        # the list and emits a ``voice_status state=degraded`` frame
        # so clients can show a "Voice degraded — using fallback TTS"
        # banner. Empty list -> ``voice_status state=unavailable`` and
        # the surface goes quiet but with a banner instead of silent
        # failure. ``whisper`` is the OpenAI ``/audio/speech`` REST
        # endpoint (cheap mp3) which most installs already have a key
        # for; ``elevenlabs`` / ``cartesia`` / ``azure`` activate when
        # the matching credential is stored in the vault.
        "fallback_tts_providers": ["whisper"],
        # When a realtime provider dies mid-session (OpenAI 1013
        # insufficient_quota, Gemini 429 quota), the router can either
        # keep streaming chunked TTS from the dead session's residual
        # text (``"whisper"`` — legacy v2026.5.31 behavior, mp3
        # ``tts_chunk`` frames) or morph the whole session in place
        # to the chained STT→LLM→TTS pipeline (``"chained"`` — S4
        # acceptance, keeps the call alive on Deepgram + ElevenLabs).
        # The chained path requires ``DEEPGRAM_API_KEY`` and
        # ``ELEVENLABS_API_KEY`` in the vault; when either is missing
        # the router degrades to ``"whisper"`` regardless of this
        # setting so the user still gets audible feedback.
        "fallback_mode": "chained",
        # Provider pair the router activates when ``fallback_mode``
        # is ``"chained"``. Operators can pin different providers
        # (e.g. ``groq_whisper`` STT + ``cartesia`` TTS) via
        # ``~/.feral/settings.json``; the runtime reads these from
        # ``audio.chained_fallback`` and passes them straight into
        # ``open_chained_session``'s ``provider_opts``.
        "chained_fallback": {
            "stt_provider": "deepgram",
            "tts_provider": "elevenlabs",
        },
    },
    "vision": {
        "enabled": False,
        "max_frame_kb": 512,
        "scene_cooldown": 10,
    },
    "features": {
        "streaming": False,
        "proactive": False,
        "self_learning": True,
        "multi_agent": True,
    },
    "memory": {
        # Pluggable vector-store backend. One of sqlite_vec (default),
        # chroma, qdrant, or any registered community backend.
        "backend": "sqlite_vec",
        "backend_config": {},
        # v2026.5.34 (PR 2 — feat/memory-v2-truth). The four memory-v2
        # subsystems each carry an ``enabled`` flag so an operator can
        # disable any one of them in ``settings.json`` without
        # reverting the release. Cluster-level flags default ON
        # because the post-canary documented behaviour is "all four
        # active"; the per-feature spec defaults are the safe
        # production tunables. Disable knobs live under each subkey.
        "decay": {
            # D11 — Ebbinghaus-with-SM-2 decay + active forgetting.
            # When ``enabled`` is true the brain runs a background
            # sweep every ``cadence_seconds`` that recalculates each
            # episode's ``decay_factor`` and marks the ones below
            # ``forget_threshold`` as forgotten. ``retention_days``
            # is the grace period before a forgotten episode (plus
            # its chunks + FTS rows) is hard-deleted from disk.
            "enabled": True,
            "cadence_seconds": 3600,
            "decay_rate": 0.001,
            "forget_threshold": 0.05,
            "access_boost_factor": 0.1,
            "retention_days": 365,
        },
        "sync": {
            # D12 — P2P federated sync scheduler. When ``enabled`` is
            # true the SyncScheduler runs every ``cadence_seconds``,
            # walks every known peer, and reconciles operations.
            # Per-peer exponential backoff starts at
            # ``backoff_initial_seconds`` and caps at
            # ``backoff_max_seconds``. ``peer_timeout_seconds`` is
            # the handshake / round-trip ceiling before a peer is
            # declared unreachable.
            "enabled": True,
            "cadence_seconds": 30,
            "peer_timeout_seconds": 10,
            "backoff_initial_seconds": 5,
            "backoff_max_seconds": 300,
            "heartbeat_interval_seconds": 15,
            "heartbeat_miss_threshold": 3,
        },
        "kg": {
            # F1 — Unified knowledge graph. When ``unified`` is true
            # writes go to the typed entity-relation graph
            # exclusively; the flat ``knowledge`` table is renamed to
            # ``knowledge__deprecated`` on first boot of this release
            # and dropped in the next release. Old data is migrated
            # into the unified graph automatically.
            "unified": True,
        },
        "compaction": {
            # F2 — Real session compaction. When ``enabled`` is true,
            # ``compact_session`` promotes summarisable turns into
            # episode rows (with participants, time_range, summary,
            # key_entities, source_turn_ids metadata). The brain
            # auto-fires compaction either at the end of a session or
            # once a session has accumulated ``turns_threshold`` new
            # turns since the last compaction.
            "enabled": True,
            "turns_threshold": 20,
        },
    },
    "security": {
        "node_api_key": "",
    },
    "skills": {
        "enabled": [],
        "disabled": [],
        "external_directories": [],
    },
    "nodes": {
        "auto_connect": True,
    },
    "ui": {
        "theme": "dark",
        "show_debug": False,
    },
    # Pairing access mode (Mode A LAN / Mode B localhost / Mode C remote).
    # Default is "localhost" to preserve the historical loopback-only
    # behavior on existing installs; the /setup wizard prompts the user
    # to pick LAN or remote (Tailscale) explicitly.
    "access": {
        "pairing_mode": "localhost",
        "remote_provider": None,
        "tailscale": {
            "funnel": True,
            "tailnet_url": "",
        },
    },
    # Per-brain identity. Populated lazily on first read by
    # ``ConfigLoader.brain_id`` so existing installs upgrade in place
    # without a settings rewrite. Clients use this to refuse to talk
    # to a different brain after pairing.
    "meta": {
        "setup_complete": False,
        "brain_id": "",
    },
}


def feral_home() -> Path:
    """Resolve the FERAL user config directory (XDG-compliant on Linux)."""
    env_home = os.environ.get("FERAL_HOME")
    if env_home:
        return Path(env_home)

    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "feral"

    return Path.home() / ".feral"


def feral_data_home() -> Path:
    """Resolve the FERAL data directory (XDG-compliant on Linux)."""
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "feral"
    return Path.home() / ".feral"


def load_settings() -> dict:
    """Lightweight module-level helper that returns the merged settings
    dict from ``~/.feral/settings.json`` (+ project + env overrides).

    Built for callers that just need to *read* a single value without
    pulling in the full ``ConfigLoader`` lifecycle (which also touches
    the encrypted vault, derives fallback providers, mirrors env
    overrides, etc.). The router's whisper-TTS fallback consults this
    to pick an alternate provider; missing settings degrade silently.
    """
    try:
        loader = ConfigLoader()
        return loader.discover() or {}
    except Exception:
        logger.debug("load_settings fallback to defaults", exc_info=True)
        return copy.deepcopy(DEFAULT_SETTINGS)


class ConfigLoader:
    """
    Loads and merges FERAL configuration from multiple sources.
    """

    def __init__(self, project_dir: Optional[str] = None):
        self.user_home = feral_home()
        self.data_home = feral_data_home()
        self.project_dir = Path(project_dir) if project_dir else Path.cwd()
        self._merged: dict = {}
        self._sources: list[dict] = []
        self._credentials: dict = {}
        self._setup_complete = False

    def discover(self) -> dict:
        """
        Load and merge all config sources. Returns the merged settings dict.
        """
        self._merged = copy.deepcopy(DEFAULT_SETTINGS)
        self._sources = []

        # Layer 1: User config (~/.feral/settings.json)
        user_path = self.user_home / "settings.json"
        self._load_and_merge(user_path, "user")

        # Layer 2: Project config (.feral/settings.json)
        project_path = self.project_dir / ".feral" / "settings.json"
        self._load_and_merge(project_path, "project")

        # Layer 3: Local config (.feral/settings.local.json) — gitignored
        local_path = self.project_dir / ".feral" / "settings.local.json"
        self._load_and_merge(local_path, "local")

        # Layer 4: Environment variable overrides
        self._apply_env_overrides()

        # Unify vision flag: the settings UI historically wrote to
        # ``features.vision`` while the env/export path read from
        # ``vision.enabled``. Treat either being truthy as "on" and mirror
        # the coalesced value back into BOTH keys so the rest of the
        # system sees a single source of truth regardless of which path
        # the operator used. Same for ``features.proactive`` (kept in
        # sync with itself, but we formalise the contract).
        self._unify_feature_flags()

        # Load credentials separately
        self._load_credentials()

        # Auto-derive fallback providers from stored keys if not explicitly set
        self._merged.setdefault("llm", {})
        self._merged["llm"]["fallback_providers"] = self._derive_fallback_providers()

        # Check if setup has been completed
        self._setup_complete = self._check_setup_complete()

        sources_desc = ", ".join(s.get("_source", "?") for s in self._sources)
        logger.info(f"Config loaded from: [{sources_desc}] | Setup complete: {self._setup_complete}")
        return self._merged

    def _load_and_merge(self, path: Path, source: str):
        if not path.exists():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            data["_source"] = source
            self._sources.append(data)
            self._deep_merge(self._merged, data)
            logger.debug(f"Loaded config from {path}")
        except Exception as e:
            logger.warning(f"Failed to load config from {path}: {e}")

    def _apply_env_overrides(self):
        """Map FERAL_* environment variables to config keys."""
        env_map = {
            "OPENAI_API_KEY": None,  # handled by credentials
            "FERAL_LLM_PROVIDER": ("llm", "provider"),
            "FERAL_LLM_MODEL": ("llm", "model"),
            "FERAL_LLM_BASE_URL": ("llm", "base_url"),
            "FERAL_LLM_DAILY_BUDGET_USD": ("llm", "daily_budget_usd"),
            "FERAL_LLM_DAILY_SPEND_USD": ("llm", "daily_spend_usd"),
            "FERAL_LLM_BUDGET_TIGHT_RATIO": ("llm", "budget_tight_ratio"),
            # ``FERAL_VISION_ENABLED`` is applied to ``vision.enabled``
            # here; ``_unify_feature_flags`` mirrors it into
            # ``features.vision`` so every consumer sees the same truth.
            "FERAL_VISION_ENABLED": ("vision", "enabled"),
            "FERAL_VISION_MAX_FRAME_KB": ("vision", "max_frame_kb"),
            "FERAL_STREAMING": ("features", "streaming"),
            "FERAL_PROACTIVE": ("features", "proactive"),
            "FERAL_MULTI_AGENT": ("features", "multi_agent"),
            "FERAL_SCENE_COOLDOWN": ("vision", "scene_cooldown"),
            "FERAL_STT_PROVIDER": ("audio", "stt_provider"),
            "FERAL_STT_MODEL": ("audio", "stt_model"),
            "FERAL_TTS_PROVIDER": ("audio", "tts_provider"),
            "FERAL_TTS_MODEL": ("audio", "tts_model"),
            "FERAL_TTS_VOICE": ("audio", "tts_voice"),
            "NODE_API_KEY": ("security", "node_api_key"),
        }

        for env_key, config_path in env_map.items():
            value = os.environ.get(env_key)
            if value is None or config_path is None:
                continue
            section, key = config_path
            if section not in self._merged:
                self._merged[section] = {}
            # Type coercion
            if isinstance(self._merged[section].get(key), bool):
                self._merged[section][key] = value.lower() in ("true", "1", "yes")
            elif isinstance(self._merged[section].get(key), int):
                try:
                    self._merged[section][key] = int(value)
                except ValueError:
                    pass
            else:
                self._merged[section][key] = value

    def _unify_feature_flags(self):
        """Coalesce ``features.vision`` and ``vision.enabled`` into one truth.

        Before W-A6 the settings UI wrote ``features.vision`` while the
        boot-time ``export_as_env()`` read ``vision.enabled``. That drift
        meant toggling vision off in the UI persisted, but the next
        restart exported ``FERAL_VISION_ENABLED=false`` based on the
        OTHER key and still started the ScreenLoop. We now take the
        logical OR of the two on read and mirror the result back into
        both, so whichever path the operator uses, the runtime agrees.
        """
        features = self._merged.setdefault("features", {})
        vision = self._merged.setdefault("vision", {})

        features_vision = features.get("vision")
        vision_enabled = vision.get("enabled")

        def _truthy(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.lower() in ("true", "1", "yes", "on")
            return bool(v)

        # If either key is explicitly set, prefer the truthy one.
        if features_vision is None and vision_enabled is None:
            unified = False
        else:
            unified = _truthy(features_vision) or _truthy(vision_enabled)

        features["vision"] = unified
        vision["enabled"] = unified

    def _load_credentials(self):
        """Load credentials from BlindVault (authoritative store).

        Environment variables override vault values only when explicitly
        set in the process environment (ops / CI convenience). Legacy
        plaintext ``credentials.json`` is never read here — opening the
        vault triggers W9 migration into ``credentials.enc`` when needed.
        """
        self._credentials = {}
        cred_path = self.user_home / "credentials.json"
        if cred_path.exists():
            logger.warning(
                "Deprecated plaintext credentials.json at %s — ConfigLoader "
                "no longer reads this file directly; credentials are loaded "
                "from the encrypted vault (migration runs on vault init).",
                cred_path,
            )

        try:
            from security.vault import BlindVault

            vault = BlindVault(vault_path=str(cred_path))
            for key in vault.list_keys():
                value = vault.get_credential(key)
                if isinstance(value, str) and value.strip():
                    self._credentials[key] = value.strip()
        except Exception as exc:
            logger.warning("Failed to load credentials from vault: %s", exc)

        # Env overrides — only when the variable is explicitly set.
        _api_key_envs = (
            "OPENAI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
            "GEMINI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY",
            "DASHSCOPE_API_KEY", "EXA_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY",
            "BRAVE_API_KEY", "GITHUB_TOKEN", "SPOTIFY_CLIENT_ID", "NOTION_TOKEN",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "FERAL_WHOOP_TOKEN", "FERAL_OURA_TOKEN",
        )
        for env_key in _api_key_envs:
            value = os.environ.get(env_key)
            if value is not None and str(value).strip():
                self._credentials[env_key] = str(value).strip()

        # Skill-specific keys from FERAL_KEY_* pattern
        for key, value in os.environ.items():
            if key.startswith("FERAL_KEY_") and str(value).strip():
                skill_id = key[10:].lower()  # FERAL_KEY_web_search -> web_search
                self._credentials.setdefault("skill_keys", {})
                self._credentials["skill_keys"][skill_id] = str(value).strip()

    def _derive_fallback_providers(self) -> list[str]:
        """Auto-populate fallback_providers from providers that have stored keys."""
        existing = self._merged.get("llm", {}).get("fallback_providers") or []
        if existing:
            return existing

        _KEY_MAP = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "groq": "GROQ_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "xai": "XAI_API_KEY",
            "cohere": "COHERE_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }

        primary = self._merged.get("llm", {}).get("provider", "openai").lower()
        providers = []
        for prov, key_name in _KEY_MAP.items():
            if prov == primary:
                continue
            key = os.environ.get(key_name, "").strip()
            if not key and key_name == "GEMINI_API_KEY":
                key = os.environ.get("GOOGLE_API_KEY", "").strip()
            if not key:
                key = self._credentials.get(key_name, "").strip() if isinstance(self._credentials.get(key_name), str) else ""
            if key:
                providers.append(prov)
        return providers

    def _check_setup_complete(self) -> bool:
        """Check if the full setup has been done (LLM key + identity)."""
        if self._merged.get("meta", {}).get("setup_complete"):
            return True
        has_llm_key = bool(
            self._credentials.get("OPENAI_API_KEY")
            or self._credentials.get("ANTHROPIC_API_KEY")
            or self._credentials.get("GOOGLE_API_KEY")
            or self._credentials.get("GROQ_API_KEY")
            or self._credentials.get("OPENROUTER_API_KEY")
            or self._credentials.get("DEEPSEEK_API_KEY")
            or self._credentials.get("MOONSHOT_API_KEY")
            or self._credentials.get("DASHSCOPE_API_KEY")
            or self._merged.get("llm", {}).get("provider") == "ollama"
        )
        if not has_llm_key:
            return False
        user_md = self.user_home / "USER.md"
        if not user_md.exists():
            return False
        content = user_md.read_text().strip()
        if not content or "Tell your agent about yourself" in content:
            return False
        if "My name is" not in content and len(content) < 50:
            return False
        return True

    @staticmethod
    def _deep_merge(base: dict, overlay: dict):
        """Recursively merge overlay into base, overlay wins on conflicts."""
        for key, value in overlay.items():
            if key.startswith("_"):
                continue
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                ConfigLoader._deep_merge(base[key], value)
            else:
                base[key] = value

    # ─── Public API ───

    @property
    def settings(self) -> dict:
        if not self._merged:
            self.discover()
        return self._merged

    @property
    def credentials(self) -> dict:
        return self._credentials

    @property
    def setup_complete(self) -> bool:
        return self._setup_complete

    def get(self, section: str, key: str, default=None):
        return self._merged.get(section, {}).get(key, default)

    @property
    def access_pairing_mode(self) -> str:
        """Resolved pairing access mode (Mode A "local" / Mode B
        "localhost" / Mode C "remote").

        Defaults to "localhost" when not set so legacy installs (no
        access namespace in settings.json) keep their existing
        loopback-only behavior. Idempotent with the same property
        added in PR #55 / phone-as-peer; whichever PR merges first,
        the other rebases cleanly because the implementation is
        identical.
        """
        mode = self._merged.get("access", {}).get("pairing_mode", "localhost")
        if mode not in ("local", "localhost", "remote"):
            return "localhost"
        return mode

    @property
    def access_remote_url(self) -> str:
        """Public-reachable URL for Mode C (Tailscale Funnel).

        Populated by ``feral access remote-up`` after running
        ``tailscale funnel <port> on``. Empty string means Mode C is
        configured but not yet live; the pair URL resolver MUST treat
        empty as "remote unavailable" rather than emitting a loopback
        URL silently.
        """
        access = self._merged.get("access", {}) or {}
        ts = access.get("tailscale", {}) or {}
        return str(ts.get("tailnet_url", "") or "")

    def get_credential(self, key: str, default: str = "") -> str:
        return self._credentials.get(key, default)

    @property
    def brain_id(self) -> str:
        """Stable per-brain UUID. Generated lazily on first access and
        persisted under ``meta.brain_id`` so subsequent boots return the
        same value. Phone clients refuse to re-pair against a different
        brain by comparing the QR's ``brain_id`` to this one.
        """
        existing = self._merged.get("meta", {}).get("brain_id", "")
        if isinstance(existing, str) and existing:
            return existing
        new_id = str(uuid4())
        self.update_settings("meta", "brain_id", new_id)
        return new_id

    def get_skill_key(self, skill_id: str) -> Optional[str]:
        return self._credentials.get("skill_keys", {}).get(skill_id)

    # ─── Write API ───

    def save_user_settings(self, settings: dict):
        """Write settings to the user config file."""
        self.user_home.mkdir(parents=True, exist_ok=True)
        path = self.user_home / "settings.json"
        with open(path, "w") as f:
            json.dump(settings, f, indent=2)
        logger.info(f"User settings saved to {path}")

    def save_credentials(self, credentials: dict):
        """Persist credentials to the W9 encrypted BlindVault.

        W24b (v2026.5.0): the pre-W9 implementation wrote the plaintext
        ``~/.feral/credentials.json`` alongside the encrypted vault,
        leaking every API key to disk as a P0 security regression. The
        legacy file is no longer written under any condition — the
        encrypted ``~/.feral/credentials.enc`` is now the sole on-disk
        store. The in-memory ``self._credentials`` dict is still updated
        so ``export_as_env`` / ``get_credential`` / boot-time providers
        observe the new values without waiting for a reload.

        Skill-keys (the nested ``skill_keys`` dict) are kept in memory
        only, which matches the HTTP-route behaviour that has always
        skipped the vault for them.
        """
        self.user_home.mkdir(parents=True, exist_ok=True)
        self._credentials.update(credentials)

        flat_creds = {
            key: value
            for key, value in credentials.items()
            if key != "skill_keys" and isinstance(value, str) and value
        }
        if not flat_creds:
            return

        try:
            from security.vault import BlindVault
        except Exception as exc:  # pragma: no cover — import-time failure
            logger.error(
                "save_credentials: vault unavailable (%s); refusing to "
                "persist to plaintext — credentials kept in memory only.",
                exc,
            )
            return

        # Route through a vault anchored on ``self.user_home`` so tests
        # (and any consumer that relocates user_home away from
        # ``feral_home()``) keep the encrypted payload inside the
        # expected directory. The BlindVault maps ``*.json`` → ``*.enc``
        # internally, so this never creates a plaintext file.
        vault = BlindVault(vault_path=str(self.user_home / "credentials.json"))
        for key, value in flat_creds.items():
            vault.set_credential(key, value)
        logger.info(
            "Credentials saved to encrypted vault (%d key(s))", len(flat_creds)
        )

    def update_settings(self, section: str, key: str, value):
        """Update a single setting and persist to user config."""
        if section not in self._merged:
            self._merged[section] = {}
        self._merged[section][key] = value

        # Load existing user settings and update
        user_path = self.user_home / "settings.json"
        user_settings = {}
        if user_path.exists():
            try:
                with open(user_path) as f:
                    user_settings = json.load(f)
            except Exception:
                pass
        if section not in user_settings:
            user_settings[section] = {}
        user_settings[section][key] = value

        # Keep ``features.vision`` and ``vision.enabled`` in lockstep on
        # write so the next ``discover()`` cannot observe split truth
        # (see ``_unify_feature_flags``).
        if section == "features" and key == "vision":
            user_settings.setdefault("vision", {})["enabled"] = bool(value)
            self._merged.setdefault("vision", {})["enabled"] = bool(value)
        elif section == "vision" and key == "enabled":
            user_settings.setdefault("features", {})["vision"] = bool(value)
            self._merged.setdefault("features", {})["vision"] = bool(value)

        self.save_user_settings(user_settings)

    def mark_setup_complete(self):
        """Mark that initial setup has been completed."""
        self.update_settings("meta", "setup_complete", True)
        self._setup_complete = True

    def discover_skills_directories(self) -> list[Path]:
        """Find all directories that may contain skill manifests."""
        dirs = []
        # Built-in
        builtin = Path(__file__).parent.parent / "skills" / "manifests"
        if builtin.exists():
            dirs.append(builtin)
        # User-installed
        user_skills = self.user_home / "skills"
        if user_skills.exists():
            dirs.append(user_skills)
        # Project-local
        project_skills = self.project_dir / ".feral" / "skills"
        if project_skills.exists():
            dirs.append(project_skills)
        # External directories from config
        for ext_dir in self._merged.get("skills", {}).get("external_directories", []):
            p = Path(ext_dir)
            if p.exists():
                dirs.append(p)
        return dirs

    def export_as_env(self) -> dict[str, str]:
        """Export settings as environment variables for backward compatibility."""
        env = {}
        llm = self._merged.get("llm", {})
        env["FERAL_LLM_PROVIDER"] = llm.get("provider", "openai")
        env["FERAL_LLM_MODEL"] = llm.get("model", "gpt-4o-mini")
        if llm.get("base_url"):
            env["FERAL_LLM_BASE_URL"] = llm["base_url"]

        # Audio subsystem — propagate every ``audio.*`` key into the
        # env vars that AudioPipeline / voice clients read. Before
        # this change the settings tree was silently ignored because
        # AudioPipeline's constructor only consulted the FERAL_STT_*
        # / FERAL_TTS_* environment variables directly.
        audio = self._merged.get("audio", {}) or {}
        if audio.get("stt_provider"):
            env["FERAL_STT_PROVIDER"] = str(audio["stt_provider"])
        if audio.get("stt_model"):
            env["FERAL_STT_MODEL"] = str(audio["stt_model"])
        if audio.get("tts_provider"):
            env["FERAL_TTS_PROVIDER"] = str(audio["tts_provider"])
        if audio.get("tts_model"):
            env["FERAL_TTS_MODEL"] = str(audio["tts_model"])
        if audio.get("tts_voice"):
            env["FERAL_TTS_VOICE"] = str(audio["tts_voice"])

        vision = self._merged.get("vision", {})
        features = self._merged.get("features", {})
        # Vision flag is coalesced by ``_unify_feature_flags`` but we
        # re-apply the OR here defensively so a caller that skipped
        # discover() still exports a sensible value.
        vision_on = bool(vision.get("enabled", False)) or bool(features.get("vision", False))
        env["FERAL_VISION_ENABLED"] = str(vision_on).lower()
        env["FERAL_VISION_MAX_FRAME_KB"] = str(vision.get("max_frame_kb", 512))

        env["FERAL_STREAMING"] = str(features.get("streaming", False)).lower()
        env["FERAL_PROACTIVE"] = str(features.get("proactive", False)).lower()
        env["FERAL_MULTI_AGENT"] = str(features.get("multi_agent", True)).lower()

        env["NODE_API_KEY"] = self._merged.get("security", {}).get("node_api_key", "")

        # Credentials — LLMs + messaging channels
        credential_env_keys = (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
            "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
            "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY",
            "TAVILY_API_KEY", "BRAVE_API_KEY", "EXA_API_KEY",
            "SERPER_API_KEY", "PERPLEXITY_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CSE_ID",
            "GITHUB_TOKEN", "SPOTIFY_CLIENT_ID",
            "FERAL_TELEGRAM_BOT_TOKEN",
            "FERAL_SLACK_BOT_TOKEN", "FERAL_SLACK_APP_TOKEN", "FERAL_SLACK_SIGNING_SECRET",
            "FERAL_DISCORD_BOT_TOKEN",
            "FERAL_WHATSAPP_PHONE_NUMBER_ID", "FERAL_WHATSAPP_ACCESS_TOKEN",
            "FERAL_WHATSAPP_VERIFY_TOKEN", "FERAL_WHATSAPP_APP_SECRET",
        )
        for cred_key in credential_env_keys:
            if self._credentials.get(cred_key):
                env[cred_key] = self._credentials[cred_key]

        return env

    def to_client_safe_dict(self) -> dict:
        """Return settings safe to send to the client (no credentials)."""
        safe = dict(self._merged)
        safe.pop("security", None)
        safe["setup_complete"] = self._setup_complete
        safe["has_llm_key"] = bool(
            self._credentials.get("OPENAI_API_KEY")
            or self._credentials.get("ANTHROPIC_API_KEY")
            or self._credentials.get("GOOGLE_API_KEY")
            or self._credentials.get("GROQ_API_KEY")
        )
        safe["has_skill_keys"] = list(self._credentials.get("skill_keys", {}).keys())
        safe["skill_directories"] = [str(d) for d in self.discover_skills_directories()]
        return safe
