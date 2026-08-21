# Installing FERAL

FERAL runs on your own machine. Nothing about your conversations,
memory, or files leaves it unless you configure a cloud provider and
use it.

## What you need

- macOS 13 or later, or Linux
- Python 3.11 or later, **with SQLite FTS5 compiled in**

That second requirement is not a formality. FERAL's memory store creates
full-text search tables while starting; without FTS5 it does not degrade,
it does not start. Many stock Python builds ship without it.

```
pip install feral-ai
feral doctor
```

Run `feral doctor` before anything else. If the **SQLite FTS5** row is
red, stop and fix your interpreter; nothing else will work. Doctor names
the interpreter it found and what to do about it.

## Optional extras

Local speech is opt-in and off by default:

```
pip install 'feral-ai[stt]'      # local speech to text
pip install 'feral-ai[tts]'      # local speech synthesis
```

Installing the package is only half of it. The models are downloaded
separately, and `feral doctor` reports those as two different states.
See [voice.md](voice.md).

## Next

[first-run.md](first-run.md).
