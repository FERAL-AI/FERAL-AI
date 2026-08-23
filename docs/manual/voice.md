# Voice

Press the **Voice** control in the top right, or open the command
palette and pick **Start voice session**.

## Cloud or local

Cloud voice needs an API key and sends your audio to that provider.
Local voice runs entirely on your machine and sends nothing anywhere.

```
feral voice                 # what is configured, and whether it can run
```

## Setting up local voice

Two separate steps, and it is normal to have done only the first:

```
pip install 'feral-ai[stt]'                                    # 1. the engine
python -m voice.local_models fetch-faster-whisper base         # 2. the model

pip install 'feral-ai[tts]'                                    # 1. the engine
python -m voice.local_models fetch-piper en_US-lessac-medium   # 2. the voice
```

`feral doctor` reports these distinctly. "installed, no model downloaded
yet" is an ℹ, because that is the ordinary state after installing the
extra. If you have *selected* a local engine and its model is missing,
that becomes a ⚠, because voice will produce nothing when you speak.

## If you chose local, FERAL keeps it local

When a local engine fails and the only fallback available is a cloud
service, FERAL stops instead of quietly switching. It tells you that is
what happened and why.

This is the point of choosing local, so it is not treated as an error to
route around.

## Use a private OpenAI-compatible transcription service

The classic `whisper` voice mode can send microphone audio to an endpoint you
operate while the normal FERAL orchestrator continues to own the agent turn.
This is useful when the brain and speech model run on different machines:

```sh
export FERAL_STT_PROVIDER=openai-compatible
export FERAL_STT_ENDPOINT=http://speech-host:8000/v1/audio/transcriptions
export FERAL_STT_MODEL=whisper-v3:turbo
export FERAL_STT_TIMEOUT_SECONDS=300
```

An API key is not required. If that server requires bearer authentication, set
`FERAL_STT_API_KEY` separately. FERAL deliberately does not reuse
`OPENAI_API_KEY` for a compatible endpoint.

Endpoint selection is fail-closed: if the URL is missing, invalid, or the
request fails, that audio is not rerouted to OpenAI. The default remains
`FERAL_STT_PROVIDER=openai`, so existing cloud configurations are unchanged.

Clients should request `voice_mode: "whisper"` in `voice_session_start` for
this STT-to-orchestrator path. TTS is independent; a client may speak the
normal assistant text locally instead of configuring server-side TTS.

## What the orb is telling you

The orb tracks the actual state of the turn: listening, thinking,
speaking. If something goes wrong it turns to the alert state and the
banner explains what, rather than the panel disappearing.

## See also

- [troubleshooting.md](troubleshooting.md) if voice produces no sound
