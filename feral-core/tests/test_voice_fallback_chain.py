"""Ordered realtime fallback chain, terminating at the chained pipeline.

The defect
==========
The user's model of voice provider selection is a chain: pick a
provider, and when it fails, fall through to the next one, ending at
the local chained pipeline rather than at failure.

What the router actually did was resolve exactly ONE provider.
``VoiceRouter._preferred_realtime_provider()`` returned a single string
from ``FERAL_VOICE_PROVIDER`` or the scalar ``audio.realtime_primary``,
and ``audio.realtime_providers`` -- the ordered list the WebUI Settings
page writes -- was read by nothing at all. It was allowlisted as a
known-dead key in ``tests/test_settings_keys_have_readers.py``.

So ``["gemini", "openai"]`` meant nothing: with Gemini down the router
went straight to the whisper batch path instead of trying OpenAI.

Surface policy
==============
The second half is that the right default order depends on WHO is
asking. An iOS phone or a pair of glasses needs realtime for latency;
a desktop can afford the chained local pipeline and keep the audio on
the machine. The router therefore has to know the requesting surface at
resolve time, which it previously had no way of doing (``node_type``
arrives in the HUP ``node_register`` payload, which the router never
sees).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from voice.router import (
    _ENV_VOICE_PROVIDER,
    SURFACE_LOCAL_FIRST,
    SURFACE_REALTIME_FIRST,
    VoiceRouter,
)


def _write_audio_settings(tmp_path, monkeypatch, audio: dict) -> None:
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"audio": audio}))


@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    """A router that never reads the developer's real ~/.feral."""
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(tmp_path, monkeypatch, {})
    return tmp_path


def _router(*, openai_up=True, gemini_up=True, chained=None):
    r = VoiceRouter(
        realtime_proxy=MagicMock(available=openai_up),
        audio_pipeline=MagicMock(),
    )
    r.set_gemini_proxy(MagicMock(available=gemini_up))
    if chained is not None:
        r.set_chained_pipeline(chained)
    return r


# ── The list key is now read ─────────────────────────────────────

def test_realtime_providers_list_is_read(tmp_path, monkeypatch):
    """``audio.realtime_providers`` reaches the router in order."""
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(
        tmp_path, monkeypatch, {"realtime_providers": ["gemini", "openai"]},
    )
    assert VoiceRouter.realtime_chain_for_surface("phone")[:2] == [
        "gemini", "openai",
    ]


def test_chain_falls_through_to_the_second_entry(tmp_path, monkeypatch):
    """``["gemini", "openai"]`` with Gemini down must land on OpenAI.

    This is the whole bug: pre-fix the router resolved a single
    provider and a dead Gemini went to the whisper path, never to the
    next entry in the operator's list.
    """
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(
        tmp_path, monkeypatch, {"realtime_providers": ["gemini", "openai"]},
    )
    r = _router(openai_up=True, gemini_up=False)
    r.set_session_voice_mode("s1", "realtime")
    assert r._resolve_session_provider("s1") == "openai"


def test_chain_falls_through_for_nodes_too(tmp_path, monkeypatch):
    """Both call sites walk the chain, not just the web one."""
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(
        tmp_path, monkeypatch, {"realtime_providers": ["gemini", "openai"]},
    )
    r = _router(openai_up=True, gemini_up=False)
    r.register_voice_config("n1", {"supports_realtime": True})
    assert r._resolve_provider("n1") == "openai"


def test_scalar_primary_leads_the_list(tmp_path, monkeypatch):
    """``realtime_primary`` is the pick; the list is the rest of the chain."""
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(tmp_path, monkeypatch, {
        "realtime_primary": "gemini_live",
        "realtime_providers": ["openai", "gemini"],
    })
    chain = VoiceRouter.realtime_chain_for_surface("phone")
    assert chain[:2] == ["gemini", "openai"]
    assert chain.count("gemini") == 1


def test_env_override_accepts_an_ordered_list(tmp_path, monkeypatch):
    _write_audio_settings(tmp_path, monkeypatch, {})
    monkeypatch.setenv(_ENV_VOICE_PROVIDER, "gemini,openai")
    assert VoiceRouter.realtime_chain_for_surface("phone")[:2] == [
        "gemini", "openai",
    ]


def test_unknown_provider_names_are_dropped_not_walked(tmp_path, monkeypatch):
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(
        tmp_path, monkeypatch,
        {"realtime_providers": ["not_a_provider", "openai"]},
    )
    chain = VoiceRouter.realtime_chain_for_surface("phone")
    assert "not_a_provider" not in chain
    assert chain[0] == "openai"


# ── The chain terminates at chained, not at failure ──────────────

def test_chain_terminates_at_chained_when_pipeline_is_wired(isolated_settings):
    """Every realtime provider down still ends at the local pipeline."""
    r = _router(openai_up=False, gemini_up=False, chained=MagicMock())
    r.set_session_voice_mode("s1", "realtime")
    assert r._resolve_session_provider("s1") == "chained"


def test_chain_terminates_at_whisper_without_a_pipeline(isolated_settings):
    """No chained pipeline wired: the honest end of the chain is whisper."""
    r = _router(openai_up=False, gemini_up=False)
    r.set_session_voice_mode("s1", "realtime")
    assert r._resolve_session_provider("s1") == "whisper"


def test_chained_is_always_the_last_link(isolated_settings):
    for surface in ("phone", "glasses", "desktop", ""):
        assert VoiceRouter.realtime_chain_for_surface(surface)[-1] == "chained"


# ── Surface-aware default order ──────────────────────────────────

def test_ios_surface_prefers_realtime(isolated_settings):
    """The user's rule: iOS clients use realtime for speed."""
    chain = VoiceRouter.realtime_chain_for_surface("phone")
    assert chain[0] in ("openai", "gemini")
    assert chain.index("chained") == len(chain) - 1


def test_glasses_surface_prefers_realtime(isolated_settings):
    assert VoiceRouter.realtime_chain_for_surface("glasses")[0] != "chained"


def test_desktop_surface_prefers_local(isolated_settings):
    """A desktop can afford the local pipeline, so it leads."""
    assert VoiceRouter.realtime_chain_for_surface("desktop")[0] == "chained"


def test_unknown_surface_keeps_the_realtime_first_default(isolated_settings):
    """An unknown surface must not silently become local-first.

    Guessing "desktop" for a surface we could not identify would route
    a phone through the slow path on the strength of a guess.
    """
    assert VoiceRouter.realtime_chain_for_surface("")[0] != "chained"


def test_surface_tables_do_not_overlap():
    assert not (SURFACE_REALTIME_FIRST & SURFACE_LOCAL_FIRST)


def test_desktop_node_routes_local_but_phone_node_routes_realtime(
    isolated_settings,
):
    """Same brain, same settings, two surfaces, two answers."""
    r = _router(openai_up=True, gemini_up=True, chained=MagicMock())
    r.register_voice_config("mac-1", {"supports_realtime": True})
    r.register_voice_config("iphone-1", {"supports_realtime": True})
    r.set_node_surface("mac-1", "desktop")
    r.set_node_surface("iphone-1", "phone")
    assert r._resolve_provider("mac-1") == "chained"
    assert r._resolve_provider("iphone-1") == "openai"


def test_explicit_env_override_beats_the_surface_policy(
    isolated_settings, monkeypatch,
):
    """An operator typing FERAL_VOICE_PROVIDER means it, on any surface."""
    monkeypatch.setenv(_ENV_VOICE_PROVIDER, "openai")
    r = _router(openai_up=True, gemini_up=True, chained=MagicMock())
    r.register_voice_config("mac-1", {"supports_realtime": True})
    r.set_node_surface("mac-1", "desktop")
    assert r._resolve_provider("mac-1") == "openai"


# ── How the surface becomes known ────────────────────────────────

def test_surface_from_voice_config_payload(isolated_settings):
    """A client that declares node_type in voice_config is believed."""
    r = _router()
    r.register_voice_config("n1", {"supports_realtime": True, "node_type": "Glasses"})
    assert r._node_surface("n1") == "glasses"


def test_surface_from_capability_registry(isolated_settings):
    """The registry already holds every connected node's HUP node_type."""
    from memory.capability_registry import CapabilityRegistry

    registry = CapabilityRegistry()
    registry.register_node("n1", node_type="phone", platform="ios", skills=[])
    r = _router()
    r.set_capability_registry(registry)
    assert r._node_surface("n1") == "phone"


def test_explicit_set_node_surface_wins_over_the_registry(isolated_settings):
    from memory.capability_registry import CapabilityRegistry

    registry = CapabilityRegistry()
    registry.register_node("n1", node_type="desktop", platform="darwin", skills=[])
    r = _router()
    r.set_capability_registry(registry)
    r.set_node_surface("n1", "glasses")
    assert r._node_surface("n1") == "glasses"


def test_session_surface_comes_from_its_bound_node(isolated_settings):
    r = _router()
    r.set_node_surface("iphone-1", "phone")
    r.bind_node_to_session("iphone-1", "sess-1")
    assert r._session_surface("sess-1") == "phone"


def test_unknown_node_surface_is_empty_not_guessed(isolated_settings):
    """No plumbing, no answer. The router must not invent a surface."""
    r = _router()
    assert r._node_surface("never-seen") == ""


def test_stop_node_voice_forgets_the_surface(isolated_settings):
    """A disconnected node must not leave its surface behind.

    Node ids are reused across reconnects, and a stale "glasses" entry
    would keep pinning a later desktop node to the realtime path.
    """
    import asyncio

    r = VoiceRouter(audio_pipeline=MagicMock())
    r.set_node_surface("n1", "phone")
    assert r._node_surface("n1") == "phone"
    asyncio.run(r.stop_node_voice("n1"))
    assert r._node_surface("n1") == ""


# ── Legacy single-provider accessor is unchanged ─────────────────

def test_preferred_realtime_provider_still_returns_the_explicit_pick(
    tmp_path, monkeypatch,
):
    """Back-compat: the scalar accessor reports only an EXPLICIT pick.

    It must not start reporting the shipped ``realtime_providers``
    default, or ``feral doctor`` and the setup wizard would claim the
    operator chose a provider they never touched.
    """
    monkeypatch.delenv(_ENV_VOICE_PROVIDER, raising=False)
    _write_audio_settings(tmp_path, monkeypatch, {})
    assert VoiceRouter._preferred_realtime_provider() == ""
    _write_audio_settings(tmp_path, monkeypatch, {"realtime_primary": "gemini_live"})
    assert VoiceRouter._preferred_realtime_provider() == "gemini"
