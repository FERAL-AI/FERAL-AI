"""One engine must not be local in one module and cloud in another.

`voice/provider_registry.py` spells the local Whisper engine
`faster_whisper`. `perception/audio_pipeline.py` spells it
`faster-whisper`, and so does the documented `FERAL_STT_PROVIDER` value
an operator is actually told to type.

`requires_credential` compared the raw string, so measured before this
change:

    _is_local_provider('stt', 'faster-whisper')  -> False
    _is_local_provider('stt', 'faster_whisper')  -> True

The consequence is not cosmetic. `VoiceRouter._operator_chose_local_voice`
is built on that answer, and it gates the refusal that stops a
local-only session from falling back to a cloud service. An operator who
set the documented spelling was recorded as having chosen a cloud
provider, so the privacy refusal never fired and their audio could be
sent off the machine with no warning.
"""

from __future__ import annotations

import pytest

from perception.audio_pipeline import _LOCAL_STT_PROVIDERS
from voice.provider_registry import (
    LOCAL_STT_PROVIDERS,
    LOCAL_TTS_PROVIDERS,
    canonical_provider,
    is_local_provider,
    requires_credential,
)
from voice.router import _is_local_provider

# Every spelling of a local STT engine that appears anywhere in the tree
# or in the documented env var.
LOCAL_STT_SPELLINGS = [
    "faster-whisper", "faster_whisper", "FASTER-WHISPER", " faster-whisper ",
    "local", "whisper-local", "whisper_local", "whispercpp", "whisper-cpp",
]
LOCAL_TTS_SPELLINGS = ["piper", "PIPER", "local", "macos_say", "macos-say", "say"]

CLOUD_STT = ["openai", "deepgram", "groq", "assemblyai", "elevenlabs"]
CLOUD_TTS = ["openai", "elevenlabs", "deepgram", "cartesia"]


@pytest.mark.parametrize("name", LOCAL_STT_SPELLINGS)
def test_every_local_stt_spelling_is_local(name):
    assert is_local_provider("stt", name), f"{name!r} is not recognised as local"
    assert not requires_credential("stt", name), f"{name!r} was said to need an API key"


@pytest.mark.parametrize("name", LOCAL_TTS_SPELLINGS)
def test_every_local_tts_spelling_is_local(name):
    assert is_local_provider("tts", name), f"{name!r} is not recognised as local"
    assert not requires_credential("tts", name)


@pytest.mark.parametrize("name", LOCAL_STT_SPELLINGS)
def test_the_router_agrees_with_the_registry(name):
    """The router's own helper is what gates the privacy refusal."""
    assert _is_local_provider("stt", name) is is_local_provider("stt", name)


@pytest.mark.parametrize("name", CLOUD_STT)
def test_a_cloud_stt_is_never_rewritten_into_a_local_one(name):
    assert not is_local_provider("stt", name), f"{name!r} was treated as local"
    assert requires_credential("stt", name)


@pytest.mark.parametrize("name", CLOUD_TTS)
def test_a_cloud_tts_is_never_rewritten_into_a_local_one(name):
    assert not is_local_provider("tts", name), f"{name!r} was treated as local"


def test_the_two_modules_do_not_disagree_about_any_name():
    """The drift guard: audio_pipeline and the registry must agree.

    These are two independently maintained name sets for the same
    engines, in different packages, and that is exactly how they came
    apart.
    """
    disagreements = [
        name for name in _LOCAL_STT_PROVIDERS if not is_local_provider("stt", name)
    ]
    assert not disagreements, (
        "audio_pipeline calls these local but the voice registry does not: "
        f"{disagreements}"
    )


def test_canonical_ids_are_the_registry_ids():
    for name in LOCAL_STT_SPELLINGS:
        assert canonical_provider("stt", name) in LOCAL_STT_PROVIDERS
    for name in LOCAL_TTS_SPELLINGS:
        assert canonical_provider("tts", name) in LOCAL_TTS_PROVIDERS


def test_an_unknown_name_is_normalised_but_not_invented():
    assert canonical_provider("stt", "Some-New-Engine") == "some_new_engine"
    assert not is_local_provider("stt", "Some-New-Engine")


def test_empty_and_missing_names_are_not_local():
    for value in ("", "   ", None):
        assert not is_local_provider("stt", value)
        assert not is_local_provider("tts", value)
        assert requires_credential("stt", value)


def test_an_unknown_kind_is_not_local():
    assert not is_local_provider("wat", "piper")
    assert requires_credential("wat", "piper")
