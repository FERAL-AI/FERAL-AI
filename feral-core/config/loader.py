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
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from uuid import uuid4

logger = logging.getLogger("feral.config")

# Secrets that ``export_as_env`` can emit. They are excluded from the
# runtime re-export in ``update_settings`` for the same reason
# ``api/state.py::_should_export_runtime_env_key`` excludes them at boot:
# a channel token belongs in the config object that needs it, not in
# process-global env where every subprocess inherits it.
_NEVER_REEXPORT_ENV_KEYS = frozenset({
    "NODE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "EXA_API_KEY",
    "SERPER_API_KEY",
    "PERPLEXITY_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CSE_ID",
    "GITHUB_TOKEN",
    "SPOTIFY_CLIENT_ID",
    "FERAL_TELEGRAM_BOT_TOKEN",
    "FERAL_SLACK_BOT_TOKEN",
    "FERAL_SLACK_APP_TOKEN",
    "FERAL_SLACK_SIGNING_SECRET",
    "FERAL_DISCORD_BOT_TOKEN",
    "FERAL_WHATSAPP_PHONE_NUMBER_ID",
    "FERAL_WHATSAPP_ACCESS_TOKEN",
    "FERAL_WHATSAPP_VERIFY_TOKEN",
    "FERAL_WHATSAPP_APP_SECRET",
})

# Single source of truth for the streaming default.
#
# There used to be two, and they disagreed. ``DEFAULT_SETTINGS`` said
# False (and ``export_as_env`` published ``FERAL_STREAMING=false`` into
# os.environ from it) while ``Orchestrator.__init__`` read
# ``os.environ.get("FERAL_STREAMING", "true")``. So the answer to "is
# streaming on" depended on whether the config loader had run first in
# that process: an orchestrator built without a loader streamed, one
# built after ``export_as_env`` did not. On a fresh FERAL_HOME the
# loader wins, so ``handle_command_stream`` emitted zero
# ``stream_delta`` frames and quietly delegated every turn to the
# non-stream path. ``tests/perf/test_lane08_live_traces.py``'s parity
# trace compared an empty assembled string against a real reply and
# passed only when an earlier test had left the variable set.
#
# The False was correct when it was written and was then left behind.
# History: v0.4.0 (dce5ede60) added the runtime gate as opt-in,
# ``os.environ.get("THEORA_STREAMING", "")``, i.e. OFF unless the
# operator exported it. The v0.5.0 config scaffold (a258a92a6) wrote
# this settings default to match. Four days later 256be0fcd
# ("streaming-first loops") flipped the runtime default to "true" and
# did not touch the settings side, so the two have disagreed ever
# since and the settings side, which publishes into os.environ, was
# the one that won at boot.
#
# ON is the resolved answer, not just the newer one. Streaming is the
# better-covered path today (multi-agent hand-off, plan mode, the
# pending-approval gate, forced tools, parallel tool execution and
# cross-provider failover all have explicit stream-side parity, pinned
# by tests/test_stream_nonstream_parity.py), and the chained voice
# pipeline reads these token deltas to start speaking sentence 1 while
# the model is still writing sentence 2 (voice/llm_stream_tap.py).
#
# Flipping this default surfaced a real gap, which has since been
# closed: neither SSE route billed a streamed turn, so streamed spend
# never reached ``cost_events`` and a configured per-call-site cap could
# not trip. The chat-completions route did not send
# ``stream_options.include_usage`` and the Anthropic route discarded the
# ``message_delta`` usage block outright. Both now record once per turn
# (``agents/llm_provider.py``), and an endpoint that rejects
# ``stream_options`` is retried once without it and remembered, so the
# turn survives even where usage cannot be captured.
DEFAULT_STREAMING = True

# Single source of truth for the multi-agent default.
#
# Same defect as ``DEFAULT_STREAMING`` above, pointing the other way.
# ``DEFAULT_SETTINGS`` said True and ``export_as_env`` published
# ``FERAL_MULTI_AGENT=true`` from it, while ``Orchestrator.__init__``
# read ``os.environ.get("FERAL_MULTI_AGENT", "false")``. So whether the
# multi-agent path answered a turn depended on whether the config
# loader had run first in that process. It is not a toggle when the two
# readers disagree, it is a race.
#
# History. d447a2c87 (v0.9, 2026-04-03) introduced the runtime gate as
# ``THEORA_MULTI_AGENT`` defaulting to "true", i.e. ON. 4df5fc1cc
# (2026-04-07, "4 critical bugs found during end-to-end audit") flipped
# that literal to "false" on the stated grounds that "multi-agent was
# enabled by default, bypassing all tool use". Three days later
# c7ac82c20 (v1.2.0, 2026-04-10) added the settings side, all of it ON:
# ``features.multi_agent: True`` here, ``FERAL_MULTI_AGENT`` defaulting
# to True in ``export_as_env``, and the dashboard toggle rendered as
# ``config.features?.multi_agent ?? true``. It did not touch the
# runtime literal, so the two have disagreed ever since, and the
# settings side, which publishes into os.environ at boot, is the one
# that wins on a real install.
#
# ON is the resolved answer, and the April rationale for OFF no longer
# describes the code. ``AgentWorker`` runs a full LLM function-calling
# tool loop (``get_tools`` -> ``llm.chat(tools=...)`` ->
# ``SkillExecutor.execute``), and that loop was already present in
# d447a2c87 with a ``get_tools`` byte-identical to today's, so whatever
# the April audit hit, it was not the absence of tool calling. Since
# then the multi-agent branch has been built out as the default path on
# purpose: 1063b3925 (2026-08-01) routed worker tool calls through
# ``ToolRunner.enforce_plan_mode`` and ``enforce_safety``, proven
# against a live brain, explicitly because "features.multi_agent
# defaults to True, so this is the primary text chat path rather than
# an edge case"; the WS3 stream-parity block in
# ``Orchestrator._handle_command_stream_impl`` mirrors the non-stream
# hand-off so enabling it does not change behaviour by client; and
# ``_pop_multi_agent_attribution`` exists only because this branch
# returns before the single-agent loop, so on a default profile it is
# the branch that has to report the model and token usage.
#
# Turning the runtime literal into this constant changes nothing on an
# install where the loader runs (it already saw "true"). What it fixes
# is the loader-less process, embedded and unit-test construction of
# ``Orchestrator``, which was silently taking a different chat path
# from production.
DEFAULT_MULTI_AGENT = True

DEFAULT_SETTINGS = {
    "version": "0.4.0",
    "llm": {
        "provider": "openai",
        # Empty on purpose: ``LLMProvider.__init__`` falls through to
        # ``_default_model_for`` which reads the live model catalog. A
        # literal here pins every new install to whatever was current
        # when it was written -- this shipped ``gpt-4o-mini``, whose
        # Oct-2023 training cutoff made it fabricate 2023 dates for
        # "schedule X at 10am" on a 2026 machine. Roadmap 3.5 P0 bans
        # hardcoded model literals for exactly this reason.
        "model": "",
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
        # Adaptive per-turn model routing (agents/llm_router.py). When on
        # (default), each chat turn is graded for difficulty and routed to
        # a tier: trivial turns drop to a cheaper SAME-provider model,
        # substantive/agentic turns use the configured model, and a bad/
        # empty answer escalates one tier and retries. Set false to pin a
        # single model. The auto path never switches providers — that's
        # opt-in via ``tier_map``.
        "adaptive_routing": True,
        # Operator overrides for tier targets, keyed by call_site → tier →
        # {provider, model}. The escape hatch for cross-provider tiering
        # (e.g. route cheap chat to a local Ollama model or a budget
        # provider). Empty = use the safe same-provider defaults.
        "tier_map": {},
        # Optional pin of the default tier per call_site
        # ({"chat": "premium", "routing": "cheap", ...}). Empty = built-in
        # conservative defaults; the chat path overrides this per-turn via
        # the difficulty classifier.
        "call_site_tiers": {},
        # Local-first routing: when true, the cheap tier on chat/routing/
        # vision prefers a local model (privacy + zero marginal cost).
        # ``local_model`` names the target; when empty and the primary is
        # already a local engine, that engine is kept.
        "local_first": False,
        "local_model": {},
    },
    "audio": {
        "stt_provider": "openai",
        "stt_model": "whisper-1",
        "tts_provider": "openai",
        "tts_model": "tts-1",
        "tts_voice": "nova",
        # Operator's preferred ORDER of realtime providers, written by
        # the WebUI Settings page (``feral-client-v2``
        # ``src/pages/Settings.jsx``).
        #
        # Read by ``voice/router.py`` ``_configured_realtime_chain()``,
        # which walks it as an ordered fallback chain: the first entry
        # that is actually up serves the session, and the chain
        # terminates at the chained STT->LLM->TTS pipeline (local
        # engines included) rather than at failure. An explicit pick in
        # ``audio.realtime_primary`` (or ``FERAL_VOICE_PROVIDER``) leads
        # the chain; this list is the fallthrough behind it.
        #
        # Ordering realtime providers is a separate question from
        # whether realtime should be tried before the local pipeline at
        # all -- that one is answered per surface (see
        # ``VoiceRouter.realtime_chain_for_surface``: phones and glasses
        # lead with realtime for latency, desktops lead with local).
        "realtime_providers": ["openai", "gemini"],
        # OpenAI Realtime model id passed to the RealtimeProxy when
        # opening a session. Operator-overridable via
        # ``feral setup`` (voice preflight) or the WebUI Voice card;
        # the runtime falls back to ``RealtimeProxy.DEFAULT_MODEL``
        # when this key is unset (Lane U2).
        # Defaults to the mini tier on cost. Realtime audio is billed per
        # audio token in both directions and the full model is roughly 3x
        # the mini on both, so a default of the full model quietly makes
        # every voice turn three times more expensive than it needs to be
        # for most usage. An operator who wants the full model sets this
        # key; the picker lists both.
        #
        # Name taken from providers/model_catalog.json, which is refreshed
        # from the live provider endpoint, rather than from documentation.
        "realtime_model": "gpt-realtime-mini",
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
    # Voice-mode settings the phone Settings panel writes. The chained
    # picks here WIN over ``audio.chained_fallback`` (see
    # ``voice/router.py::_resolve_chained_config``): that block is the
    # headless / wizard default, this one is an explicit UI choice.
    "voice": {
        # Empty strings mean "not chosen here" so the resolver falls
        # through to ``audio.chained_fallback`` and then to the shipped
        # provider defaults. Writing real values here would silently
        # override a wizard pick on every install.
        "chained": {
            "stt_provider": "",
            "tts_provider": "",
            "stt_model": "",
            "tts_model": "",
            "tts_voice": "",
            "tts_voice_id": "",
        },
        # Server-side voice activity detection (Silero, see
        # ``voice/vad.py``). This is what ends an utterance; before it
        # existed the pipeline waited for the client to stop sending
        # and then waited again, roughly 2.3s of dead air per turn.
        #
        # Turning it off is supported and costs latency, not
        # correctness: the packet-absence silence timer takes over.
        "vad": {
            "enabled": True,
            # Speech starts above ``threshold`` and stops below
            # ``neg_threshold``. The gap is hysteresis so one quiet
            # frame mid-word does not read as end-of-utterance.
            "threshold": 0.5,
            "neg_threshold": 0.35,
            # Continuous silence that ends the utterance. Under about
            # 200ms, ordinary pauses between words start cutting
            # people off; over about 500ms the reply feels sluggish.
            "min_silence_ms": 300,
            # Ignore blips shorter than this so a cough does not open
            # an utterance that then has to be transcribed to nothing.
            "min_speech_ms": 96,
            # Speech detected while the assistant is talking cancels
            # the turn. Requires the client's echo cancellation to be
            # on, or the assistant's own voice re-triggers it.
            "barge_in": True,
        },
    },
    "vision": {
        "enabled": False,
        "max_frame_kb": 512,
        "scene_cooldown": 10,
    },
    "features": {
        # See ``DEFAULT_STREAMING`` above. Do not write a literal here:
        # ``agents/orchestrator.py`` imports the same constant, and the
        # two defaults disagreeing is what broke the stream path.
        "streaming": DEFAULT_STREAMING,
        "proactive": False,
        "self_learning": True,
        # See ``DEFAULT_MULTI_AGENT`` above. Same rule as streaming: no
        # literal here, because ``agents/orchestrator.py`` imports the
        # same constant and the two disagreeing is the whole bug.
        "multi_agent": DEFAULT_MULTI_AGENT,
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
        # Tool-approval tier the operator picks in `feral setup`
        # (capabilities step) or the timeline API. Exported to
        # ``FERAL_AUTONOMY`` by ``export_as_env`` because that env var
        # is the single source both ``agents/tool_runner.py`` and
        # ``security/exec_mode.current_autonomy_mode()`` read. Before
        # the export existed this key was written by the wizard and
        # read by nothing, so picking "strict" still ran "hybrid".
        # Valid: strict | hybrid | loose.
        "autonomy_mode": "hybrid",
    },
    # Agent tool-loop budget (v2026.6.11). 0 = unlimited iterations — the
    # loop is governed by the no-progress guard (identical failing tool
    # call repeated) plus the wall-clock backstop below, not by an
    # arbitrary count. Set max_tool_iterations > 0 to impose a hard limit.
    "agents": {
        "max_tool_iterations": 0,
        "tool_loop_max_seconds": 900,
    },
    "skills": {
        # ``skills.enabled`` / ``skills.disabled`` used to live here.
        # Nothing ever wrote them (no route, no wizard step, no client)
        # and nothing ever read them: skill availability comes from the
        # manifests discovered by ``discover_skills_directories`` plus
        # ``security/sandbox_policy.py``'s ``blocked_skill_ids``. They
        # were removed rather than wired, because two competing sources
        # of truth for "is this skill on" is how the vision-flag drift
        # happened (see ``_unify_feature_flags``).
        "external_directories": [],
    },
    # Coding-harness knobs. Each of these shipped as an env var only, so
    # they were configurable per-process but not persistable: an operator
    # who wanted read-before-edit enforced had to re-export the variable
    # on every launch. Mirroring them into settings makes the choice
    # survive a restart.
    #
    # Precedence is unchanged and deliberately env-first. The env var
    # lands in this section via ``_apply_env_overrides`` and is then
    # re-exported verbatim by ``export_as_env``, so a shell that sets
    # ``FERAL_READ_BEFORE_EDIT=enforce`` still beats settings.json. The
    # defaults below are copied from the readers, so an install that
    # never touches this section behaves exactly as before.
    "coding": {
        # off | warn | enforce. ``skills/file_state.py`` (MODE_WARN).
        "read_before_edit": "warn",
        # on | off. ``skills/call_context.py``.
        "tool_call_context": "on",
        # Cost guard for the fallback edit matchers, which are
        # O(file_lines x needle_lines). ``skills/impl/coding_tools.py``
        # via ``skills/edit_matchers.py`` DEFAULT_MAX_CONTENT_LINES /
        # DEFAULT_MAX_NEEDLE_LINES.
        "edit_max_content_lines": 4000,
        "edit_max_needle_lines": 400,
        # Empty means "$FERAL_HOME/checkpoints"; only a non-empty value
        # is exported, because ``skills/checkpoints.py::checkpoint_root``
        # treats any truthy value as an outright override.
        "checkpoint_dir": "",
        "checkpoint_retention_days": 14,
        # 8 MiB, matching ``skills/checkpoints.py``.
        "checkpoint_max_blob_bytes": 8388608,
        # on | off. ``skills/diagnostics.py``.
        "post_edit_diagnostics": "on",
        # Seconds. ``skills/diagnostics.py`` floors this at 0.5.
        "diagnostics_timeout": 5,
        # Seconds a turn may sit idle before the tool loop gives up.
        # ``agents/tool_runner.py``.
        "turn_idle_seconds": 180,
    },
    # External coding agents FERAL can drive as subprocesses over ACP
    # (the Agent Client Protocol). Every key here is read by
    # ``bridges/catalog.py::external_agent_settings``, which is the only
    # place in ``bridges/`` that touches settings at all.
    "external_agents": {
        # Which agent the ``external_agent`` skill picks when the caller
        # does not name one. Only ``opencode`` is installable by FERAL;
        # ``claude_code`` and ``codex`` need their own Node shims because
        # neither CLI speaks ACP natively.
        "default_agent": "opencode",
        # Absolute path override for the opencode binary. Empty means
        # "look on PATH, then in ~/.opencode/bin", which is where the
        # official installer puts it when run with --no-modify-path.
        "opencode_bin": "",
        # Version the setup step installs. Pinned on purpose: this is a
        # 45 MB binary with its own permission engine, not a thing to
        # resolve to "latest" on a user's machine at install time.
        # ``cli/setup/steps/external_agents.py`` reads it.
        "opencode_version": "1.18.10",
        # Seconds a permission request from an external agent may sit
        # unanswered before ``bridges/permissions.py`` rejects it. Floored
        # at 5; there is no value meaning "allow".
        "permission_timeout_seconds": 120,
        # Spawn external agents in ``security/env_jail.py`` (a throwaway
        # HOME plus a model-key allowlist) instead of the operator's own
        # environment. On by default: an unjailed agent can read
        # ``~/.claude``, ``~/.codex`` and ``~/.ssh`` for free. Turn it off
        # only if the agent authenticates by subscription login rather
        # than an API key, since that login lives in the real HOME.
        # ``bridges/catalog.py::external_agent_settings`` reads it.
        "env_jail": True,
    },
    # Pairing access mode (Mode A LAN / Mode B localhost / Mode C remote).
    # Default is "localhost" to preserve the historical loopback-only
    # behavior on existing installs; the /setup wizard prompts the user
    # to pick LAN or remote (Tailscale) explicitly.
    "access": {
        "pairing_mode": "localhost",
        # ``remote_provider`` used to sit here and had no reader. Three
        # sites still write it as provenance (``cli/setup/network.py``,
        # ``cli/setup/steps/network.py``, ``api/routes/access.py``);
        # those writes are unaffected, the key just no longer poses as a
        # configurable default.
        "tailscale": {
            # ``funnel`` used to sit here defaulting to True and was
            # never written OR read: the remote-up flow calls
            # ``tailscale.funnel_enable()`` unconditionally, so the flag
            # gated nothing.
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
    """Resolve the FERAL data directory (XDG-compliant on Linux).

    ``FERAL_HOME`` wins here exactly as it does in :func:`feral_home`.
    It did not before, which gave an operator who relocated their
    install a split brain: settings and vault under the new root,
    databases still under ``~/.feral``. It was also a test-isolation
    hazard, since setting ``FERAL_HOME`` alone left data writes
    pointing at the real home directory.
    """
    env_home = os.environ.get("FERAL_HOME")
    if env_home:
        return Path(env_home)

    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "feral"
    return Path.home() / ".feral"


def local_timezone_name() -> str:
    """Best-effort IANA timezone name for the host machine.

    The scheduler and the orchestrator's per-turn time context both need
    a real IANA key (e.g. ``America/Los_Angeles``) so ``ZoneInfo`` can
    anchor wall-clock schedules — a bare offset name like ``PDT`` is not
    enough. Resolution order (first that yields a valid ZoneInfo wins):

      1. ``FERAL_TIMEZONE`` env override (explicit operator control).
      2. ``TZ`` env var, when it names a valid zone.
      3. The ``/etc/localtime`` symlink target (macOS + most Linux),
         parsed back to its IANA key.
      4. ``datetime.now().astimezone().tzinfo.key`` when the platform
         exposes a ZoneInfo-backed local tz.
      5. ``UTC`` as the safe fallback.

    Never raises — every probe is best-effort.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZoneInfo

    def _valid(name: str) -> bool:
        try:
            _ZoneInfo(name)
            return True
        except Exception:
            return False

    env_tz = os.environ.get("FERAL_TIMEZONE", "").strip()
    if env_tz and _valid(env_tz):
        return env_tz

    tz_env = os.environ.get("TZ", "").strip()
    if tz_env and _valid(tz_env):
        return tz_env

    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            cand = link.split("zoneinfo/", 1)[1].lstrip("/")
            if cand and _valid(cand):
                return cand
    except OSError:
        pass

    try:
        key = getattr(_dt.now().astimezone().tzinfo, "key", None)
        if isinstance(key, str) and key and _valid(key):
            return key
    except Exception:
        pass

    return "UTC"


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


def _merge_patch(target: dict, patch: dict) -> dict:
    """Apply ``patch`` to ``target`` under RFC 7386 JSON Merge Patch rules.

    B8 helper for :meth:`ConfigLoader.save_user_settings`; see that
    docstring for why the whole-file replacement it supersedes was
    destroying settings.

    Rules, restated because the choice is load-bearing:

      * ``patch`` value is ``None``  -> delete the key from the result,
      * both sides are objects       -> recurse,
      * anything else                -> the patch value replaces.

    An absent key is therefore "leave alone" and an explicit value is
    "make it this", which is what lets the setup wizard both preserve
    what it does not render and still clear what it does. Arrays fall
    into "anything else" on purpose: a settings list is a single value.

    Neither argument is mutated; the result is a new dict.
    """
    out = dict(target)
    for key, value in (patch or {}).items():
        if value is None:
            out.pop(key, None)
            continue
        current = out.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            out[key] = _merge_patch(current, value)
        else:
            out[key] = value
    return out


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

        # Repair an ``llm.base_url`` a previous setup run persisted
        # without the OpenAI-compat path suffix (see
        # ``_repair_local_base_url``). Runs after the env layer so an
        # explicit ``FERAL_LLM_BASE_URL`` is repaired too.
        self._repair_local_base_url()

        # Unify vision flag: the settings UI historically wrote to
        # ``features.vision`` while the env/export path read from
        # ``vision.enabled``. Treat either being truthy as "on" and mirror
        # the coalesced value back into BOTH keys so the rest of the
        # system sees a single source of truth regardless of which path
        # the operator used. Same for ``features.proactive`` (kept in
        # sync with itself, but we formalise the contract).
        self._unify_feature_flags()

        # Repair an access mode that contradicts the persisted bind host.
        # Installs made before the single-writer refactor can hold
        # ``pairing_mode: local`` with ``bind_host: 127.0.0.1`` — the
        # state the web "Same WiFi" button used to produce — which
        # advertises a LAN pair URL that nothing is listening on.
        self._repair_access_mode()

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
            # ``agents/learner.py`` resolves self-learning ONLY from
            # ``FERAL_SELF_LEARNING``, and only ``api/routes/config.py``
            # ever set that variable, at toggle time. So switching
            # self-learning off in Settings held until the next restart
            # and then silently reverted to on, because nothing exported
            # ``features.self_learning`` at boot. Same defect as the
            # autonomy tier below; same fix, both halves of the round
            # trip so an explicit env var still wins.
            "FERAL_SELF_LEARNING": ("features", "self_learning"),
            "FERAL_SCENE_COOLDOWN": ("vision", "scene_cooldown"),
            "FERAL_STT_PROVIDER": ("audio", "stt_provider"),
            "FERAL_STT_MODEL": ("audio", "stt_model"),
            "FERAL_TTS_PROVIDER": ("audio", "tts_provider"),
            "FERAL_TTS_MODEL": ("audio", "tts_model"),
            "FERAL_TTS_VOICE": ("audio", "tts_voice"),
            "NODE_API_KEY": ("security", "node_api_key"),
            # Keeps env > settings precedence for the autonomy tier:
            # the value lands in ``security.autonomy_mode`` here and is
            # re-exported verbatim, so an operator's FERAL_AUTONOMY
            # still wins over settings.json.
            "FERAL_AUTONOMY": ("security", "autonomy_mode"),
            # Coding harness. Env stays the override: the value lands in
            # ``coding.*`` here and ``export_as_env`` puts it straight
            # back, so an operator's shell export still wins over
            # settings.json.
            "FERAL_READ_BEFORE_EDIT": ("coding", "read_before_edit"),
            "FERAL_TOOL_CALL_CONTEXT": ("coding", "tool_call_context"),
            "FERAL_EDIT_MAX_CONTENT_LINES": ("coding", "edit_max_content_lines"),
            "FERAL_EDIT_MAX_NEEDLE_LINES": ("coding", "edit_max_needle_lines"),
            "FERAL_CHECKPOINT_DIR": ("coding", "checkpoint_dir"),
            "FERAL_CHECKPOINT_RETENTION_DAYS": ("coding", "checkpoint_retention_days"),
            "FERAL_CHECKPOINT_MAX_BLOB_BYTES": ("coding", "checkpoint_max_blob_bytes"),
            "FERAL_POST_EDIT_DIAGNOSTICS": ("coding", "post_edit_diagnostics"),
            "FERAL_DIAGNOSTICS_TIMEOUT": ("coding", "diagnostics_timeout"),
            "FERAL_TURN_IDLE_SECONDS": ("coding", "turn_idle_seconds"),
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

    # Local OpenAI-compatible providers whose base URL MUST carry the
    # ``/v1`` path prefix, because the LLM client posts the relative
    # path ``/chat/completions`` against it.
    _OPENAI_COMPAT_LOCAL_PROVIDERS = ("ollama", "lmstudio")

    def _repair_access_mode(self):
        """Make ``access.pairing_mode`` and ``network.bind_host`` agree.

        These two must be consistent or the brain advertises an address
        it is not listening on. Only the setup wizard ever wrote both;
        the web Settings button, the web Setup card, and both
        ``remote-up`` paths wrote the mode alone. Repairing here means
        an install that is already broken heals on its next boot rather
        than needing the user to notice.

        Best-effort: a failure to repair must never stop a brain from
        booting, so this swallows and logs.
        """
        try:
            from config.access_mode import repair_contradiction

            repair_contradiction(self)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("access-mode repair skipped: %s", exc)

    def _repair_local_base_url(self):
        """Re-add the ``/v1`` suffix a bare local ``llm.base_url`` is missing.

        Setup wizards up to v2026.7.31 copied the provider catalog's
        Ollama descriptor into ``llm.base_url`` verbatim, and that
        descriptor carried ``http://localhost:11434`` with no path. The
        LLM client posts the relative path ``/chat/completions`` against
        whatever base it is handed and only substitutes its own default
        when the slot is EMPTY, so the bare URL won: the brain booted
        clean, reported ``LLM: ready``, and 404'd on every chat turn.

        The catalog now ships the ``/v1`` form, but installs created
        before that fix already have the broken value persisted in
        ``~/.feral/settings.json``. Repair it on read so an existing
        install starts working again without the operator re-running
        setup or hand-editing JSON.

        Only a base URL with an empty path is touched. Anything that
        already names a path (``/v1``, ``/v1beta``, a gateway prefix)
        is the operator's deliberate choice and is left alone.
        """
        llm = self._merged.get("llm") or {}
        provider = str(llm.get("provider") or "").strip().lower()
        if provider not in self._OPENAI_COMPAT_LOCAL_PROVIDERS:
            return
        base_url = str(llm.get("base_url") or "").strip()
        if not base_url:
            return
        try:
            parsed = urlparse(base_url)
        except Exception:
            return
        if not parsed.scheme or not parsed.netloc:
            return
        if parsed.path.strip("/"):
            return
        repaired = f"{parsed.scheme}://{parsed.netloc}/v1"
        self._merged.setdefault("llm", {})["base_url"] = repaired
        logger.info(
            "config: repaired %s llm.base_url %s -> %s (missing OpenAI-compat "
            "/v1 path; every chat request would have 404'd)",
            provider, base_url, repaired,
        )

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
        vault triggers  migration into ``credentials.enc`` when needed.
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

    @staticmethod
    def _drop_unrunnable_providers(providers: list[str], *, source: str) -> list[str]:
        """Filter a failover chain down to providers the runtime can actually dial.

        ``SUPPORTED_RUNTIME_PROVIDERS`` is the set with a real adapter in
        ``agents.llm_provider``. Anything else -- an operator-written
        ``settings.json`` naming ``bedrock``, or a ``_KEY_MAP`` entry like
        ``cohere`` / ``mistral`` that has a credential but no adapter --
        used to enter the chain unchecked and burn a failover hop per turn
        logging "Provider 'x' has no runtime adapter". On a live brain this
        showed up as every chain ending in ``chat_with_failover exhausted``
        (operator report 2026-07: 14 exhausted chains, 1 successful call).

        Dropping them here rather than at dial time means the chain the
        orchestrator sees is honest. The drop is logged, never silent, so a
        typo'd or aspirational provider id is visible instead of mysterious.

        Imported lazily: ``agents.llm_provider`` imports this module at
        module scope, so a top-level import here would be a cycle.
        """
        if not providers:
            return []
        try:
            from agents.llm_provider import is_supported_runtime_provider
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("runtime-provider filter unavailable (%s); passing chain through", exc)
            return providers

        kept, dropped = [], []
        for prov in providers:
            (kept if is_supported_runtime_provider(prov) else dropped).append(prov)
        if dropped:
            logger.warning(
                "Dropping %s from the LLM failover chain (%s): no runtime adapter. "
                "Kept: %s",
                ", ".join(dropped), source, ", ".join(kept) or "(none)",
            )
        return kept

    def _derive_fallback_providers(self) -> list[str]:
        """Auto-populate fallback_providers from providers that have stored keys."""
        existing = self._merged.get("llm", {}).get("fallback_providers") or []
        if existing:
            return self._drop_unrunnable_providers(existing, source="settings.json")

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
        # ``_KEY_MAP`` above lists credentials we know how to *read*, which is
        # a wider set than the providers we can *dial* (cohere, mistral and
        # xai have no adapter today), so the derived chain needs the same
        # filter as the operator-supplied one.
        return self._drop_unrunnable_providers(providers, source="derived from stored keys")

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
        from config.access_mode import coerce

        # Single definition of what a valid mode is, shared with the
        # write path. ``coerce`` keeps the historical forgiving
        # behaviour (unknown value -> "localhost") so a hand-edited
        # settings.json cannot stop a brain from booting, and it now
        # also understands "relay".
        return coerce(self._merged.get("access", {}).get("pairing_mode", "localhost")).value

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
        """Stable per-brain UUID, generated lazily and persisted.

        A label, not a credential. This docstring used to claim phone
        clients "refuse to re-pair against a different brain by
        comparing the QR's ``brain_id`` to this one". No such check
        exists on either side, and it could not be trusted if it did: a
        uuid is unsigned, so anyone can put any value in a QR code.

        For an identity a client can actually verify, see
        :mod:`security.brain_identity`, whose ``relay_id`` is derived
        from an Ed25519 public key rather than chosen.
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
        """Merge ``settings`` into the user config file and persist it.

        B8. This used to be ``open(path, "w")`` + ``json.dump(settings)``
        with no read and no merge, so whatever the caller handed over
        became the WHOLE of settings.json. Its only non-``update_settings``
        caller is ``POST /api/setup/complete``, which hands over the
        browser wizard's form payload, so re-running the wizard deleted
        every key the wizard does not render. Those keys are not
        incidental; they are written by six other subsystems that all go
        through ``update_settings`` (read, patch one key, write back) and
        are therefore invisible to the form:

          meta.brain_id                    config/loader.py
          meta.relay_id                    security/brain_identity.py
          channels.*_allowed_senders       api/state.py _persist_pairing
          channels.*_allowed_chats         api/state.py _persist_pairing
          access.tailscale                 api/routes/access.py
          llm.fallback_providers           api/routes/llm.py
          memory.backend                   api/routes/memory.py

        The sharpest of those is the pairing allowlist: an operator who
        re-opened setup to change a model revoked every sender allowed to
        message the brain, silently.

        SEMANTICS: RFC 7386 JSON Merge Patch, via :func:`_merge_patch`.

          * a key ABSENT from ``settings`` is left exactly as it was,
          * a key PRESENT with a non-null value replaces what was there,
            so ``""``, ``false``, ``0`` and ``[]`` all still clear a value,
          * a key present with ``null`` is DELETED,
          * objects merge recursively, arrays replace wholesale.

        A blind merge would be its own defect: the wizard must remain able
        to turn something off. Under these rules it does so by SAYING so,
        and only silence means "leave alone". Arrays replace rather than
        accumulate because a list here is one value (an allowlist that
        merged element-wise could never be shortened, and would grow a
        duplicate on every write).

        Note the asymmetry with ``update_settings``, which reads the file
        itself and passes a full document down: merging that document
        against itself is a no-op, so its read-modify-write contract is
        unchanged.
        """
        self.user_home.mkdir(parents=True, exist_ok=True)
        path = self.user_home / "settings.json"

        existing: dict = {}
        if path.exists():
            try:
                with open(path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
                else:
                    logger.warning(
                        "settings.json at %s is not an object; replacing it", path,
                    )
            except (OSError, ValueError) as exc:
                # Unparseable: there is nothing to preserve, and refusing
                # to write would strand the caller (the setup route) with
                # a 500 on a file it cannot repair from the browser.
                logger.warning(
                    "settings.json at %s is unreadable (%s); writing the new "
                    "values over it", path, exc,
                )

        merged = _merge_patch(existing, settings if isinstance(settings, dict) else {})
        with open(path, "w") as f:
            json.dump(merged, f, indent=2)
        logger.info(f"User settings saved to {path}")

    def save_credentials(self, credentials: dict):
        """Persist credentials to the  encrypted BlindVault.

         (v2026.5.0): the  implementation wrote the plaintext
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

    def _env_snapshot(self) -> dict[str, str]:
        """``export_as_env()`` defensively, for diffing. Never raises."""
        try:
            return dict(self.export_as_env())
        except Exception as exc:  # pragma: no cover - export is pure today
            logger.debug("export_as_env failed during env sync: %s", exc)
            return {}

    def _publish_env_changes(self, before: dict[str, str]) -> tuple[str, ...]:
        """Push env vars whose value changed since ``before`` into os.environ.

        This closes the "setting written, env var not exported" defect at
        its funnel instead of once per surface. Whole subsystems resolve
        their config ONLY from the environment: ``security/exec_mode``
        and ``agents/tool_runner`` read ``FERAL_AUTONOMY``,
        ``agents/learner`` reads ``FERAL_SELF_LEARNING``,
        ``skills/file_state`` reads ``FERAL_READ_BEFORE_EDIT``. A write
        that lands in ``settings.json`` and stops there takes effect only
        on the next ``feral start``, which reads to the operator as the
        toggle not working.

        Only the diff is applied, so writing ``audio.tts_voice`` touches
        ``FERAL_TTS_VOICE`` and nothing else. A runtime value set by
        another subsystem (``api/state.py`` heals ``FERAL_LLM_MODEL``,
        ``api/routes/llm.py`` installs provider keys) is left alone
        unless this write genuinely changed it.

        Secrets in :data:`_NEVER_REEXPORT_ENV_KEYS` are never pushed;
        see that constant.
        """
        after = self._env_snapshot()
        changed: list[str] = []
        for name, value in after.items():
            if name in _NEVER_REEXPORT_ENV_KEYS or name.startswith("FERAL_KEY_"):
                continue
            if before.get(name) != value:
                os.environ[name] = value
                changed.append(name)
        for name in before:
            if name in after or name in _NEVER_REEXPORT_ENV_KEYS:
                continue
            if name.startswith("FERAL_KEY_"):
                continue
            os.environ.pop(name, None)
            changed.append(name)
        if changed:
            logger.debug("settings write re-exported %s", sorted(changed))
        return tuple(sorted(changed))

    def update_settings(self, section: str, key: str, value):
        """Update a single setting, persist it, and re-export its env var.

        Returns the env var names this write changed in ``os.environ``,
        which is empty for a setting that has no env mirror. See
        :meth:`_publish_env_changes` for why the re-export is here rather
        than in each of the four surfaces that write settings.
        """
        before = self._env_snapshot()
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

        # Republish to os.environ only when a brain is actually running.
        #
        # The re-export exists so a live toggle reaches env-only readers
        # without a restart, which is a real bug it fixes. But it mutates
        # global process state, and that is hostile everywhere else: the
        # setup wizard writes settings before any brain exists (the brain
        # then reads the file at boot, so publishing is pointless), and a
        # test cannot undo it, because monkeypatch can only revert writes
        # it made itself and ``delenv(name, raising=False)`` on an absent
        # variable registers no undo at all.
        #
        # That is not hypothetical: it made 28 tests fail in the full
        # suite while every one passed alone. A parametrised case leaked
        # FERAL_POST_EDIT_DIAGNOSTICS as 'enforce', and the diagnostics
        # suite then saw a disabled checker and got None from every call.
        # Disabling the publish turned 28 failures into 0.
        #
        # Gating on a live brain keeps the fix exactly where it matters
        # and removes it everywhere it only causes harm.
        if not self._brain_is_live():
            return ()
        return self._publish_env_changes(before)

    @staticmethod
    def _brain_is_live() -> bool:
        """True when a booted brain owns this process's environment.

        Checked through ``sys.modules`` rather than an import so config
        does not depend on api, and so merely importing api.state (which
        pytest collection does) is not mistaken for a running brain: the
        module is present long before ``orchestrator`` is built.
        """
        state_mod = sys.modules.get("api.state")
        state_obj = getattr(state_mod, "state", None)
        return getattr(state_obj, "orchestrator", None) is not None

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
        env["FERAL_LLM_MODEL"] = llm.get("model", "")
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
        # ``SceneAnalyzer.__init__`` resolves its VLM ONLY from these env vars
        # (perception/scene.py). Before this, ``settings.vision.provider`` and
        # ``settings.vision.model`` were written by the operator, stored on
        # disk, and read by nothing: ``_vlm_client`` stayed None, and every
        # screen frame fell through ``_call_vlm`` to ``_call_default_llm``,
        # billing the shared paid chat model.
        #
        # An operator who had explicitly selected free local vision
        # (provider=ollama, model=llava) was being charged roughly
        # $0.55-$1.12/hour for an idle machine, because the switch they set
        # was never wired to anything. Export it.
        if vision.get("provider"):
            env["FERAL_VLM_PROVIDER"] = str(vision["provider"])
        if vision.get("model"):
            env["FERAL_VLM_MODEL"] = str(vision["model"])
        if vision.get("base_url"):
            env["FERAL_VLM_BASE_URL"] = str(vision["base_url"])

        env["FERAL_STREAMING"] = str(
            features.get("streaming", DEFAULT_STREAMING)
        ).lower()
        env["FERAL_PROACTIVE"] = str(features.get("proactive", False)).lower()
        env["FERAL_MULTI_AGENT"] = str(
            features.get("multi_agent", DEFAULT_MULTI_AGENT)
        ).lower()
        # ``agents/learner.py::_self_learning_enabled`` reads
        # ``FERAL_SELF_LEARNING`` and nothing else, defaulting to true
        # when unset. Without this export the Settings toggle only held
        # for the life of the process that handled the click: the next
        # boot left the variable unset and self-learning came back on,
        # burning LLM calls on extract + summarize for an operator who
        # had explicitly turned it off.
        env["FERAL_SELF_LEARNING"] = str(features.get("self_learning", True)).lower()

        security = self._merged.get("security", {}) or {}
        env["NODE_API_KEY"] = security.get("node_api_key", "")

        # Tool-approval tier. ``agents/tool_runner.py`` and
        # ``security/exec_mode.current_autonomy_mode()`` both resolve
        # this ONLY from ``FERAL_AUTONOMY``; nothing reads
        # ``settings.security.autonomy_mode`` directly. Without this
        # export the wizard's autonomy question was a dead write and an
        # operator who picked "strict" silently ran "hybrid". Unknown
        # values fall back to the same default both readers use, so a
        # hand-edited settings file cannot widen the tier by accident.
        raw_autonomy = str(security.get("autonomy_mode", "") or "").strip().lower()
        env["FERAL_AUTONOMY"] = (
            raw_autonomy if raw_autonomy in ("strict", "hybrid", "loose") else "hybrid"
        )

        # Coding harness. Every one of these is read from the process
        # environment by its subsystem and from nowhere else, so the
        # export is what makes the settings section mean anything. The
        # fallbacks repeat each reader's own default so an install with
        # no ``coding`` block in settings.json exports exactly the values
        # the readers would have chosen for themselves.
        coding = self._merged.get("coding", {}) or {}
        env["FERAL_READ_BEFORE_EDIT"] = str(coding.get("read_before_edit", "warn"))
        env["FERAL_TOOL_CALL_CONTEXT"] = str(coding.get("tool_call_context", "on"))
        env["FERAL_EDIT_MAX_CONTENT_LINES"] = str(
            coding.get("edit_max_content_lines", 4000)
        )
        env["FERAL_EDIT_MAX_NEEDLE_LINES"] = str(
            coding.get("edit_max_needle_lines", 400)
        )
        # ``checkpoints.checkpoint_root`` treats ANY truthy value as an
        # outright override of ``$FERAL_HOME/checkpoints``, so an empty
        # setting must not be exported as an empty string.
        if coding.get("checkpoint_dir"):
            env["FERAL_CHECKPOINT_DIR"] = str(coding["checkpoint_dir"])
        env["FERAL_CHECKPOINT_RETENTION_DAYS"] = str(
            coding.get("checkpoint_retention_days", 14)
        )
        env["FERAL_CHECKPOINT_MAX_BLOB_BYTES"] = str(
            coding.get("checkpoint_max_blob_bytes", 8388608)
        )
        env["FERAL_POST_EDIT_DIAGNOSTICS"] = str(
            coding.get("post_edit_diagnostics", "on")
        )
        env["FERAL_DIAGNOSTICS_TIMEOUT"] = str(coding.get("diagnostics_timeout", 5))
        env["FERAL_TURN_IDLE_SECONDS"] = str(coding.get("turn_idle_seconds", 180))

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
