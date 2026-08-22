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

## What the orb is telling you

The orb tracks the actual state of the turn: listening, thinking,
speaking. If something goes wrong it turns to the alert state and the
banner explains what, rather than the panel disappearing.

## See also

- [troubleshooting.md](troubleshooting.md) if voice produces no sound
