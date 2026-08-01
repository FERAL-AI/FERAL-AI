"""On-disk store for the weights the local voice engines need.

Everything local (Silero VAD, whisper.cpp / faster-whisper models,
Piper voices) resolves through here so there is exactly one answer to
"where do the weights live" and exactly one rule about when they may
be fetched.

The rule
--------
**Weights are never downloaded mid-session.** A voice turn that
discovers a missing model fails loudly and tells the operator which
command to run. Downloads happen in ``feral setup`` or from an
explicit ``ensure_*(allow_download=True)`` call, and nowhere else.

The reason is latency honesty. A first-run download inside
``synthesize()`` turns one turn into a 90-second stall that looks
identical to a hang, and a privacy-motivated operator who picked local
engines deserves to know when bytes leave the machine.

Location
--------
``feral_home() / "models" / <family>``. ``feral_home()`` honours
``FERAL_HOME``, so tests point the whole store at a tmpdir without
touching the operator's real ``~/.feral``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("feral.voice.local_models")

# Silero VAD v5, ONNX export. MIT. ~2.2MB.
#
# Pinned to a tag rather than ``master`` so the bytes a given FERAL
# release downloads cannot change under it: the ONNX input signature
# differs between v4 (``h``/``c`` LSTM state) and v5 (a single fused
# ``state``), and silently swapping one for the other would break
# inference at runtime rather than at install time.
SILERO_VAD_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/"
    "v5.1.2/src/silero_vad/data/silero_vad.onnx"
)
SILERO_VAD_FILENAME = "silero_vad.onnx"

# Piper voices are two files: the ONNX weights and its JSON config.
# Both live under the same rounded voice name on HuggingFace.
PIPER_VOICE_BASE_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main"
)


class ModelUnavailable(RuntimeError):
    """A local engine was selected but its weights are not on disk.

    Carries the exact command that fixes it. Raised, never swallowed:
    an operator who chose local engines for privacy must not be
    silently rerouted to a cloud provider.
    """

    def __init__(self, what: str, path: Path, remedy: str):
        self.what = what
        self.path = path
        self.remedy = remedy
        super().__init__(
            f"{what} is not available locally (expected at {path}). {remedy}"
        )


@dataclass(frozen=True)
class ModelSpec:
    """One downloadable artefact."""
    family: str
    filename: str
    url: str
    # Optional lower bound on a sane download, in bytes. Guards against
    # a proxy or a 404 page being written to disk as if it were weights.
    min_bytes: int = 1024


def models_root() -> Path:
    """Root of the local model store (``$FERAL_HOME/models``)."""
    from config.loader import feral_home

    return Path(feral_home()) / "models"


def model_dir(family: str) -> Path:
    return models_root() / family


def model_path(family: str, filename: str) -> Path:
    return model_dir(family) / filename


def is_present(family: str, filename: str, *, min_bytes: int = 1024) -> bool:
    """True when the artefact exists and is not a truncated stub."""
    path = model_path(family, filename)
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def download(spec: ModelSpec, *, timeout: float = 300.0) -> Path:
    """Fetch *spec* into the store, atomically.

    Downloads to a temp file in the destination directory and renames
    on success, so an interrupted fetch can never leave a half-written
    file that :func:`is_present` would report as ready.
    """
    import httpx

    dest_dir = model_dir(spec.family)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / spec.filename

    logger.info("Downloading %s -> %s", spec.url, dest)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(dest_dir), suffix=".part")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        with httpx.stream(
            "GET", spec.url, timeout=timeout, follow_redirects=True
        ) as response:
            response.raise_for_status()
            with tmp_path.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=65536):
                    handle.write(chunk)
        size = tmp_path.stat().st_size
        if size < spec.min_bytes:
            raise RuntimeError(
                f"{spec.url} returned {size} bytes, expected at least "
                f"{spec.min_bytes} - refusing to install it as {spec.filename}"
            )
        shutil.move(str(tmp_path), str(dest))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    logger.info("Downloaded %s (%d bytes)", dest, dest.stat().st_size)
    return dest


def silero_vad_spec() -> ModelSpec:
    return ModelSpec(
        family="vad",
        filename=SILERO_VAD_FILENAME,
        url=SILERO_VAD_URL,
        min_bytes=512 * 1024,
    )


def silero_vad_path() -> Path:
    """Where the VAD weights live. May not exist."""
    return model_path("vad", SILERO_VAD_FILENAME)


def silero_vad_present() -> bool:
    return is_present("vad", SILERO_VAD_FILENAME, min_bytes=512 * 1024)


def ensure_silero_vad(*, allow_download: bool = False) -> Path:
    """Return the VAD weights path, optionally fetching them.

    ``allow_download`` defaults to False so a runtime caller can ask
    "is it here?" without ever reaching the network. Only setup passes
    True.
    """
    path = silero_vad_path()
    if silero_vad_present():
        return path
    if not allow_download:
        raise ModelUnavailable(
            "Silero VAD",
            path,
            "Run `feral setup` and pick a voice mode, or fetch it with "
            "`python -m voice.local_models fetch-vad`.",
        )
    return download(silero_vad_spec())


def piper_voice_specs(voice: str) -> tuple[ModelSpec, ModelSpec]:
    """Resolve the two artefacts for a Piper voice name.

    Piper names encode their own path: ``en_US-lessac-medium`` lives at
    ``en/en_US/lessac/medium/en_US-lessac-medium.onnx``.
    """
    parts = voice.split("-")
    if len(parts) != 3:
        raise ValueError(
            f"Piper voice {voice!r} is not in <locale>-<name>-<quality> form"
        )
    locale, name, quality = parts
    lang = locale.split("_")[0]
    base = f"{PIPER_VOICE_BASE_URL}/{lang}/{locale}/{name}/{quality}/{voice}"
    return (
        ModelSpec(
            family="piper",
            filename=f"{voice}.onnx",
            url=f"{base}.onnx",
            min_bytes=1024 * 1024,
        ),
        ModelSpec(
            family="piper",
            filename=f"{voice}.onnx.json",
            url=f"{base}.onnx.json",
            min_bytes=128,
        ),
    )


def piper_voice_present(voice: str) -> bool:
    try:
        weights, config = piper_voice_specs(voice)
    except ValueError:
        return False
    return is_present(
        weights.family, weights.filename, min_bytes=weights.min_bytes
    ) and is_present(config.family, config.filename, min_bytes=config.min_bytes)


def ensure_piper_voice(voice: str, *, allow_download: bool = False) -> Path:
    """Return the path to a Piper ``.onnx``, optionally fetching it."""
    weights, config = piper_voice_specs(voice)
    if piper_voice_present(voice):
        return model_path(weights.family, weights.filename)
    if not allow_download:
        raise ModelUnavailable(
            f"Piper voice {voice!r}",
            model_path(weights.family, weights.filename),
            "Run `feral setup` and pick local TTS, or fetch it with "
            f"`python -m voice.local_models fetch-piper {voice}`.",
        )
    download(config)
    return download(weights)


# whisper.cpp GGUF/GGML models. ``pywhispercpp`` resolves and caches
# these itself, so FERAL only needs to know whether a given size has
# already been materialised, and where.
WHISPERCPP_FAMILY = "whispercpp"


def whispercpp_model_filename(model: str) -> str:
    return f"ggml-{model}.bin"


def whispercpp_model_present(model: str) -> bool:
    """True when a whisper.cpp model of *model* size is on disk.

    Checks FERAL's own store first, then ``pywhispercpp``'s default
    cache, because an operator who already ran pywhispercpp elsewhere
    on this machine should not be made to download the same weights
    twice.
    """
    if is_present(
        WHISPERCPP_FAMILY,
        whispercpp_model_filename(model),
        min_bytes=1024 * 1024,
    ):
        return True
    cached = _pywhispercpp_cached_path(model)
    return bool(cached and cached.is_file())


def _pywhispercpp_cached_path(model: str) -> Path | None:
    """Where pywhispercpp would have cached *model*, if anywhere.

    Returns None rather than raising when pywhispercpp is not
    installed: a readiness check must never depend on the optional
    dependency being importable.

    The fallback is platform-correct. pywhispercpp resolves its cache
    through ``platformdirs``, which is
    ``~/Library/Application Support/pywhispercpp/models`` on macOS and
    ``~/.local/share/pywhispercpp/models`` on Linux (verified: the
    installed pywhispercpp 1.5.0 reports the Library path on Darwin).
    Hardcoding the Linux path meant a Mac with weights already in the
    platform cache was told to download them again.
    """
    try:
        from pywhispercpp import constants  # type: ignore

        base = Path(getattr(constants, "MODELS_DIR", ""))
    except Exception:
        import platform as _platform

        if _platform.system() == "Darwin":
            base = (
                Path.home()
                / "Library"
                / "Application Support"
                / "pywhispercpp"
                / "models"
            )
        else:
            base = Path.home() / ".local" / "share" / "pywhispercpp" / "models"
    if not str(base):
        return None
    candidate = base / whispercpp_model_filename(model)
    return candidate if candidate.exists() else None


def whispercpp_model_dir() -> Path:
    """Directory handed to pywhispercpp as its model cache."""
    path = model_dir(WHISPERCPP_FAMILY)
    path.mkdir(parents=True, exist_ok=True)
    return path


#: Approximate on-disk size per whisper.cpp model, in megabytes. Shown
#: to the operator before a download so "install local STT" is never a
#: surprise several hundred megabytes.
WHISPERCPP_MODEL_SIZES_MB: dict[str, int] = {
    "tiny": 75, "tiny.en": 75,
    "base": 142, "base.en": 142,
    "small": 466, "small.en": 466,
    "medium": 1500, "medium.en": 1500,
    "large-v3": 3100, "large-v3-turbo": 1600,
}


def whispercpp_model_size_mb(model: str) -> int:
    return WHISPERCPP_MODEL_SIZES_MB.get(model, 0)


def ensure_whispercpp_model(model: str, *, allow_download: bool = False) -> Path:
    """Return the whisper.cpp weights path, optionally fetching them.

    Delegates the fetch to ``pywhispercpp.utils.download_model`` rather
    than reimplementing the GGML URL scheme, but pins the destination to
    FERAL's own store so the weights live with the rest of the install
    and an uninstall does not leave gigabytes in a platform cache dir.
    """
    path = model_path(WHISPERCPP_FAMILY, whispercpp_model_filename(model))
    if whispercpp_model_present(model):
        cached = _pywhispercpp_cached_path(model)
        return path if path.is_file() else (cached or path)
    if not allow_download:
        raise ModelUnavailable(
            f"whisper.cpp model {model!r}",
            path,
            "Run `feral setup` and choose local voice to download it.",
        )
    from pywhispercpp.utils import download_model

    return Path(download_model(model, download_dir=str(whispercpp_model_dir())))


FASTER_WHISPER_FAMILY = "faster_whisper"

#: Approximate on-disk size per faster-whisper (CTranslate2, int8)
#: model, in megabytes. Measured for ``tiny.en`` (75MB on disk after
#: ``download_model``); the rest are scaled from the published
#: CTranslate2 conversions.
FASTER_WHISPER_MODEL_SIZES_MB: dict[str, int] = {
    "tiny": 75, "tiny.en": 75,
    "base": 145, "base.en": 145,
    "small": 484, "small.en": 484,
    "medium": 1530, "medium.en": 1530,
    "large-v2": 3090, "large-v3": 3090,
    "distil-small.en": 340, "distil-large-v3": 1510,
}


def faster_whisper_model_size_mb(model: str) -> int:
    return FASTER_WHISPER_MODEL_SIZES_MB.get(model, 0)


def faster_whisper_model_dir(model: str) -> Path:
    """FERAL's own directory for one faster-whisper model.

    A flat per-model directory rather than a HuggingFace hub cache
    tree. ``faster_whisper.download_model(size, output_dir=...)`` maps
    ``output_dir`` onto ``local_dir``, so the files land here directly
    and ``WhisperModel`` can be pointed at the directory with
    ``local_files_only=True``.
    """
    return model_dir(FASTER_WHISPER_FAMILY) / model


def _hf_hub_snapshot(model: str) -> Path | None:
    """A HuggingFace hub snapshot for *model*, if one already exists.

    An operator who ran faster-whisper elsewhere on this machine
    already has the weights; making them download a second copy of the
    same bytes into FERAL's store is a waste of the disk they were
    worried about in the first place.
    """
    hub = Path(
        os.environ.get("HF_HOME")
        or os.environ.get("HUGGINGFACE_HUB_CACHE")
        or (Path.home() / ".cache" / "huggingface")
    )
    if hub.name != "hub":
        hub = hub / "hub"
    snapshot_root = hub / f"models--Systran--faster-whisper-{model}" / "snapshots"
    if not snapshot_root.is_dir():
        return None
    for snap in snapshot_root.iterdir():
        if (snap / "model.bin").is_file():
            return snap
    return None


def faster_whisper_model_present(model: str) -> bool:
    """True when a faster-whisper (CTranslate2) model is materialised.

    faster-whisper resolves to a directory of files rather than one
    blob, so presence is "a directory holding a model.bin exists",
    either in FERAL's store or in an existing HuggingFace hub cache.
    """
    if (faster_whisper_model_dir(model) / "model.bin").is_file():
        return True
    return _hf_hub_snapshot(model) is not None


def faster_whisper_model_path(model: str) -> Path:
    """Resolve *model* to a directory ``WhisperModel`` can load.

    FERAL's own store wins over the HuggingFace cache so an uninstall
    takes the weights with it. Returns the FERAL path even when
    nothing is present yet, so callers have somewhere to report.
    """
    own = faster_whisper_model_dir(model)
    if (own / "model.bin").is_file():
        return own
    snapshot = _hf_hub_snapshot(model)
    return snapshot if snapshot is not None else own


def ensure_faster_whisper_model(
    model: str, *, allow_download: bool = False
) -> Path:
    """Return a loadable faster-whisper model directory, optionally fetching it.

    This is the piece that was missing. Without it, choosing
    faster-whisper in ``feral setup`` produced an install that refused
    every session: nothing ever passed ``allow_download=True`` and no
    fetch path existed, so
    :func:`~voice.stt_providers.faster_whisper_local.faster_whisper_available`
    reported "model not downloaded" forever.

    The fetch is delegated to ``faster_whisper.download_model`` rather
    than reimplementing the repo layout, but ``output_dir`` pins it to
    FERAL's store. Note that this is deliberately *not* the same as
    ``WhisperModel(size, download_root=...)``: ``download_root`` is a
    *cache_dir*, and constructing a ``WhisperModel`` that way will
    silently hit the network at load time, which is exactly the
    mid-session download this module exists to prevent.
    """
    if faster_whisper_model_present(model):
        return faster_whisper_model_path(model)
    dest = faster_whisper_model_dir(model)
    if not allow_download:
        raise ModelUnavailable(
            f"faster-whisper model {model!r}",
            dest,
            "Run `feral setup` and choose local voice to download it, or "
            f"fetch it with `python -m voice.local_models "
            f"fetch-faster-whisper {model}`.",
        )
    from faster_whisper import download_model

    dest.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading faster-whisper model %r -> %s", model, dest)
    path = Path(download_model(model, output_dir=str(dest)))
    if not (path / "model.bin").is_file():
        raise RuntimeError(
            f"faster-whisper download of {model!r} produced no model.bin at "
            f"{path} - refusing to report it as installed"
        )
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cli() -> int:
    """Tiny fetch CLI so setup and operators share one download path."""
    import sys

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(
            "usage: python -m voice.local_models {where|fetch-vad"
            "|fetch-piper <voice>|fetch-whispercpp <model>"
            "|fetch-faster-whisper <model>}"
        )
        return 0
    cmd = args[0]
    if cmd == "where":
        print(models_root())
        return 0
    if cmd == "fetch-vad":
        print(ensure_silero_vad(allow_download=True))
        return 0
    if cmd == "fetch-piper":
        if len(args) < 2:
            print("fetch-piper needs a voice name, e.g. en_US-lessac-medium")
            return 2
        print(ensure_piper_voice(args[1], allow_download=True))
        return 0
    if cmd == "fetch-whispercpp":
        if len(args) < 2:
            print("fetch-whispercpp needs a model name, e.g. base.en")
            return 2
        print(ensure_whispercpp_model(args[1], allow_download=True))
        return 0
    if cmd == "fetch-faster-whisper":
        if len(args) < 2:
            print("fetch-faster-whisper needs a model name, e.g. base.en")
            return 2
        print(ensure_faster_whisper_model(args[1], allow_download=True))
        return 0
    print(f"unknown command {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
