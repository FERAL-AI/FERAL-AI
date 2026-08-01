"""macOS ``say`` TTS (fully local, zero install, zero download).

Tier 0 local TTS on macOS. The synthesiser is part of the OS, it
already has whatever voice the user picked in System Settings
(including Apple's downloadable Enhanced and Premium neural voices),
there is nothing to license and nothing to fetch.

It also emits exactly the format the pipeline wants::

    say --file-format=WAVE --data-format=LEI16@24000 -o out.wav "text"

which is mono PCM16 at 24kHz, matching the chained pipeline's default
output rate, so no resampling sits between the engine and the speaker.

Measured on this machine (Apple M1, 186 installed voices)::

    text                        wall     audio    wall/audio
    "Yes."                      1.070s   0.52s      2.06
    5 words                     1.085s   1.29s      0.84
    12 words                    1.104s   3.11s      0.36
    23 words                    1.119s   6.57s      0.17

and by voice, for the same 5-word line::

    default (Premium)  1.076s     Alex      0.865s
    Samantha           0.805s     Daniel    0.547s

The shape of that table is the whole design constraint: cost is almost
entirely **fixed per invocation** (0.55s to 1.08s of process start plus
voice load, depending on the voice) and almost nothing per word.

That cuts against splitting the reply finely. Synthesising "Yes." on
its own takes twice as long as the audio it produces, so a chunk that
short underruns playback and the listener hears a gap. Hence
:attr:`min_chunk_chars` below, which the pipeline reads to decide how
much text to accumulate before asking for audio. It is set so a chunk's
spoken duration comfortably exceeds its own synthesis time.

This contradicts the intuition that per-sentence streaming always wins.
For a network TTS with a low fixed cost and a per-token stream it does.
For ``say`` it wins only on time-to-first-audio, and only if the chunks
stay big enough to keep the speaker fed.
"""

from __future__ import annotations

import asyncio
import io
import logging
import platform
import shutil
import tempfile
import wave
from pathlib import Path
from typing import AsyncIterator

from voice.tts_providers import TTSProvider, register_tts_provider

logger = logging.getLogger("feral.voice.tts.macos_say")

SAY_BINARY = "say"
DEFAULT_SAMPLE_RATE = 24000
# Bytes yielded per iteration. 100ms at 24kHz mono.
STREAM_CHUNK_BYTES = 4800

# See the module docstring: below roughly this much text, one ``say``
# invocation costs more wall time than the audio it returns will take
# to play, and the pipeline underruns.
DEFAULT_MIN_CHUNK_CHARS = 80


def say_available() -> tuple[bool, str]:
    """``(ready, reason)``. No credential, no download, no model."""
    if platform.system() != "Darwin":
        return False, "macOS only"
    if not shutil.which(SAY_BINARY):
        return False, "`say` not found on PATH"
    return True, "ready"


def available_voices(limit: int = 0) -> list[str]:
    """Installed voice names, best effort. Empty list on failure."""
    ready, _reason = say_available()
    if not ready:
        return []
    import subprocess

    try:
        proc = subprocess.run(
            [SAY_BINARY, "-v", "?"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception:
        return []
    names: list[str] = []
    for line in proc.stdout.splitlines():
        # "Alex                en_US    # Most people recognise me..."
        head = line.split("#", 1)[0].rstrip()
        if not head:
            continue
        parts = head.rsplit(None, 1)
        if len(parts) == 2 and "_" in parts[1]:
            names.append(parts[0].strip())
    if limit:
        return names[:limit]
    return names


@register_tts_provider("macos_say")
class MacOSSayTTSProvider(TTSProvider):
    """Local TTS through the macOS speech synthesiser."""

    is_local = True
    #: Raw samples, so the pipeline streams frames out as they exist
    #: instead of buffering a whole encoded file (see
    #: ``ChainedVoicePipeline._speak_chunk``).
    output_format = "pcm"

    def __init__(
        self,
        *,
        api_key: str = "",
        voice: str = "",
        rate: int = 0,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        min_chunk_chars: int = DEFAULT_MIN_CHUNK_CHARS,
        timeout: float = 60.0,
    ):
        # Accepted and ignored - the router hands ``api_key=`` to every
        # TTS constructor. Nothing local ever reads it.
        del api_key

        ready, reason = say_available()
        if not ready:
            raise RuntimeError(
                f"macOS `say` TTS is unavailable: {reason}. "
                "Pick a different TTS provider. FERAL will not silently "
                "fall back to a cloud provider."
            )
        # Empty voice means "whatever the user chose in System
        # Settings", which is almost always the right answer and is the
        # only way Enhanced / Premium voices get used without us
        # maintaining a catalogue of them.
        self._voice = (voice or "").strip()
        self._rate = int(rate or 0)
        self.sample_rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
        self.min_chunk_chars = int(min_chunk_chars or DEFAULT_MIN_CHUNK_CHARS)
        self._timeout = float(timeout)
        self._proc: asyncio.subprocess.Process | None = None

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        text = (text or "").strip()
        if not text:
            return

        with tempfile.TemporaryDirectory(prefix="feral-say-") as tmpdir:
            out_path = Path(tmpdir) / "speech.wav"
            argv = [
                SAY_BINARY,
                "--file-format=WAVE",
                f"--data-format=LEI16@{self.sample_rate}",
                "-o", str(out_path),
            ]
            if self._voice:
                argv += ["-v", self._voice]
            if self._rate > 0:
                argv += ["-r", str(self._rate)]
            # ``--`` then the text, so a reply beginning with a dash is
            # spoken rather than parsed as a flag.
            argv += ["--", text]

            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._proc = proc
            try:
                _out, err = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except asyncio.CancelledError:
                # Barge-in. Kill the synthesiser rather than leave it
                # writing a file nobody will ever play.
                self._kill(proc)
                raise
            except asyncio.TimeoutError:
                self._kill(proc)
                raise RuntimeError(
                    f"`say` did not finish within {self._timeout}s"
                )
            finally:
                self._proc = None

            if proc.returncode != 0:
                detail = (err or b"").decode("utf-8", "replace").strip()
                raise RuntimeError(
                    f"`say` failed (exit {proc.returncode}): {detail[:200]}"
                )

            for chunk in self._read_pcm(out_path):
                yield chunk

    def _read_pcm(self, path: Path):
        """Strip the WAV container and hand back raw PCM16 frames.

        The pipeline's ``pcm16`` wire format is headerless samples: the
        client builds an AudioBuffer from them directly. Shipping the
        44-byte RIFF header inside the first frame would be decoded as
        11 samples of noise at the start of every reply.
        """
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
                raise RuntimeError(
                    "`say` produced "
                    f"{handle.getnchannels()}ch/{handle.getsampwidth() * 8}-bit "
                    "audio, expected mono PCM16"
                )
            if handle.getframerate() != self.sample_rate:
                # Surface it rather than play it fast: a rate mismatch
                # here is the same failure class as the 24kHz-as-16kHz
                # Deepgram bug, and just as confusing from the outside.
                raise RuntimeError(
                    f"`say` produced {handle.getframerate()}Hz audio, "
                    f"expected {self.sample_rate}Hz"
                )
            frames_per_chunk = STREAM_CHUNK_BYTES // 2
            while True:
                data = handle.readframes(frames_per_chunk)
                if not data:
                    return
                yield data

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            logger.debug("could not kill `say`", exc_info=True)

    async def close(self) -> None:
        proc = self._proc
        if proc is not None:
            self._kill(proc)
            self._proc = None


def wav_bytes_to_pcm(data: bytes) -> bytes:
    """Helper for tests: unwrap a WAV blob to its PCM payload."""
    with wave.open(io.BytesIO(data), "rb") as handle:
        return handle.readframes(handle.getnframes())
