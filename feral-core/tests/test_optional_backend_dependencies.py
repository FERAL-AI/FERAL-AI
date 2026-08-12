"""F-10 — runtime-imported backends that no extra can install.

``mlx-lm`` and ``sentence-transformers`` are imported at runtime and appear in
neither ``dependencies`` nor any of the 33 extras, so there is no supported way
to turn either backend on.

The audit's note suggested the mlx path might be dead. It is not:
``agents/llm_provider.py:809`` calls ``create_local_engine()``, and that factory
returns ``MLXEngine`` by default on Darwin/arm64, which is the project's
flagship platform.

Re-verifying it turned up a second defect the audit does not name. Against the
current release the call site does not work at all::

    $ pip download mlx-lm --no-deps          # mlx_lm 0.31.3
    $ python -c "import inspect; from mlx_lm.generate import generate_step; \\
                 inspect.signature(generate_step).bind(None, None, temp=0.7)"
    TypeError: got an unexpected keyword argument 'temp'

``MLXEngine.generate`` passes ``temp=``, which ``mlx_lm.generate`` forwards
through ``stream_generate`` into ``generate_step``. ``generate_step`` is
keyword-only, has no ``temp`` and no ``**kwargs``; sampling moved behind
``sampler=make_sampler(temp=...)``. ``MLXEngine.generate_stream`` has the
matching problem: ``stream_generate`` yields ``GenerationResponse`` dataclasses
and the code ``str()``-ed them, so it would have streamed dataclass reprs
instead of text.

So declaring the dependency without correcting the call site would have been a
downgrade: today the user gets "mlx-lm not installed. Run: pip install mlx-lm",
which is actionable. Installed but mis-called, they would get a TypeError from
inside a thread executor. Both halves are therefore one change.

``sentence-transformers`` is the opposite case and is deliberately NOT promoted
to a runtime dependency: ``pyproject.toml`` carries a measured argument for
fastembed (~226MB installed) over a torch-backed sentence-transformers stack
(~2.5GB). It gets an extra so the documented ``FERAL_EMBED_FALLBACK=local``
mode is reachable, and a test below keeps it out of ``dependencies`` and
``[all]``.
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

import pytest

from agents.local_inference import MLXEngine


tomllib = pytest.importorskip("tomllib")

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _extras() -> dict[str, list[str]]:
    return _pyproject()["project"].get("optional-dependencies", {})


def _extras_naming(package: str) -> dict[str, str]:
    """Extras whose requirement list mentions `package`, mapped to that line."""
    found = {}
    for name, requirements in _extras().items():
        for requirement in requirements:
            if requirement.split(";")[0].strip().lower().startswith(package):
                found[name] = requirement
    return found


# ── the declaration gap ──────────────────────────────────────────


def test_mlx_lm_is_installable_through_an_extra():
    """`create_local_engine()` returns MLXEngine by default on Apple Silicon."""
    found = _extras_naming("mlx-lm")
    assert found, (
        "mlx-lm is imported by agents/local_inference.MLXEngine and declared by "
        "no extra, so the engine auto-selected on Apple Silicon cannot be "
        "installed through the package at all"
    )


def test_the_mlx_extra_is_scoped_to_the_platform_that_selects_it():
    """The marker must match `create_local_engine`'s auto-detect exactly.

    `mlx` has no wheel for Intel Macs or Linux, so an unmarked requirement
    would make `pip install feral-ai[local]` fail everywhere else.
    """
    found = _extras_naming("mlx-lm")
    assert found, "no extra declares mlx-lm"
    for extra, requirement in found.items():
        assert ";" in requirement, (
            f"[{extra}] declares mlx-lm with no environment marker: {requirement}"
        )
        marker = requirement.split(";", 1)[1].lower()
        assert "darwin" in marker, f"[{extra}] marker does not scope to macOS: {marker}"
        assert "arm64" in marker, (
            f"[{extra}] marker does not scope to Apple Silicon: {marker}"
        )


def test_sentence_transformers_is_installable_through_an_extra():
    """`FERAL_EMBED_FALLBACK=local` is documented and needs this package."""
    assert _extras_naming("sentence-transformers"), (
        "memory/embeddings.py imports sentence_transformers for the documented "
        "FERAL_EMBED_FALLBACK=local mode, and no extra installs it"
    )


def test_sentence_transformers_stays_out_of_the_default_install():
    """Guard the measured decision recorded in pyproject.toml.

    fastembed is ~226MB installed against ~2.5GB for a torch-backed
    sentence-transformers stack. Nobody should "finish the job" by promoting
    this to `dependencies` or `[all]`.
    """
    base = _pyproject()["project"]["dependencies"]
    assert not any("sentence-transformers" in r for r in base), (
        "sentence-transformers must not be a runtime dependency; it adds ~2.5GB "
        "to every install for a fallback almost nobody selects"
    )
    all_extra = _extras().get("all", [])
    assert not any("sentence-transformers" in r for r in all_extra), (
        "sentence-transformers must not be in [all] for the same reason"
    )


# ── the call site, bound against the real API ────────────────────
#
# Signatures recorded from mlx_lm 0.31.3 with:
#
#   pip download mlx-lm --no-deps && python -c "
#     import inspect
#     from mlx_lm import generate, stream_generate
#     from mlx_lm.generate import generate_step
#     print(inspect.signature(generate_step))"
#
# `test_recorded_mlx_signatures_match_the_real_package` below re-checks these
# against the installed package whenever it is importable, so they cannot go
# stale silently.

RECORDED_GENERATE_STEP_KEYWORDS = {
    "max_tokens",
    "sampler",
    "logits_processors",
    "max_kv_size",
    "prompt_cache",
    "prefill_step_size",
    "kv_bits",
    "kv_group_size",
    "quantized_kv_start",
    "prompt_progress_callback",
    "input_embeddings",
}


class _GenerationResponse:
    """Stands in for mlx_lm.generate.GenerationResponse.

    The real one is a dataclass whose `text` field holds the segment; `str()`
    of it is the dataclass repr, which is what the old code streamed.
    """

    def __init__(self, text: str):
        self.text = text

    def __repr__(self) -> str:
        return f"GenerationResponse(text={self.text!r}, token=0)"


def _install_fake_mlx_lm(monkeypatch: pytest.MonkeyPatch, calls: list[dict]):
    """Inject an mlx_lm whose kwarg handling matches the real package's."""

    def generate_step(prompt, model, **kwargs):
        unexpected = set(kwargs) - RECORDED_GENERATE_STEP_KEYWORDS
        if unexpected:
            raise TypeError(
                f"generate_step() got an unexpected keyword argument "
                f"{sorted(unexpected)[0]!r}"
            )
        return None

    def stream_generate(model, tokenizer, prompt, max_tokens=256, **kwargs):
        calls.append({"max_tokens": max_tokens, **kwargs})
        generate_step(prompt, model, max_tokens=max_tokens, **kwargs)
        for chunk in ("hello", " world"):
            yield _GenerationResponse(chunk)

    def generate(model, tokenizer, prompt, verbose=False, **kwargs):
        return "".join(r.text for r in stream_generate(model, tokenizer, prompt, **kwargs))

    def make_sampler(temp=0.0, top_p=0.0, **kwargs):
        calls.append({"sampler_temp": temp})
        return lambda logits: logits

    mlx_lm = types.ModuleType("mlx_lm")
    mlx_lm.load = lambda model_id: (object(), object())
    mlx_lm.generate = generate
    mlx_lm.stream_generate = stream_generate

    generate_mod = types.ModuleType("mlx_lm.generate")
    generate_mod.generate_step = generate_step
    generate_mod.GenerationResponse = _GenerationResponse

    sample_utils = types.ModuleType("mlx_lm.sample_utils")
    sample_utils.make_sampler = make_sampler

    monkeypatch.setitem(sys.modules, "mlx_lm", mlx_lm)
    monkeypatch.setitem(sys.modules, "mlx_lm.generate", generate_mod)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sample_utils)


@pytest.mark.asyncio
async def test_mlx_generate_binds_against_the_real_kwargs(
    monkeypatch: pytest.MonkeyPatch,
):
    """`temp=` reaches generate_step, which has no such parameter."""
    calls: list[dict] = []
    _install_fake_mlx_lm(monkeypatch, calls)

    engine = MLXEngine("mlx-community/Qwen2.5-7B-Instruct-4bit")
    engine._model = object()
    engine._tokenizer = object()
    engine.loaded = True

    text = await engine.generate("hi", max_tokens=32, temperature=0.5)
    assert text == "hello world"
    assert any(c.get("sampler_temp") == 0.5 for c in calls), (
        "temperature never reached the sampler, so the knob does nothing"
    )


@pytest.mark.asyncio
async def test_mlx_stream_yields_text_not_dataclass_reprs(
    monkeypatch: pytest.MonkeyPatch,
):
    """stream_generate yields GenerationResponse; str() of it is a repr."""
    calls: list[dict] = []
    _install_fake_mlx_lm(monkeypatch, calls)

    engine = MLXEngine("mlx-community/Qwen2.5-7B-Instruct-4bit")
    engine._model = object()
    engine._tokenizer = object()
    engine.loaded = True

    chunks = [chunk async for chunk in engine.generate_stream("hi", max_tokens=32)]
    assert chunks == ["hello", " world"], (
        f"streamed {chunks!r}; a GenerationResponse repr here means the user "
        f"sees dataclass text instead of the model's output"
    )


def test_recorded_mlx_signatures_match_the_real_package():
    """Keeps the stub above honest wherever mlx_lm is actually installed.

    Skips on Linux CI, runs on an Apple Silicon box with `feral-ai[local]`.
    Without this the stub could drift from the library and keep passing, which
    is trap 3 in CLAUDE.md.
    """
    mlx_generate = pytest.importorskip("mlx_lm.generate")
    real = inspect.signature(mlx_generate.generate_step)
    keyword_only = {
        name for name, p in real.parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert keyword_only == RECORDED_GENERATE_STEP_KEYWORDS, (
        "mlx_lm.generate_step's keyword arguments have changed; re-record them "
        "and re-check MLXEngine's call, then move the pyproject bound"
    )
    assert "temp" not in real.parameters, (
        "mlx_lm reintroduced temp=; the sampler indirection may no longer be needed"
    )
