"""The ``memory`` setup step, and the promotion of the embedding stack
into the core dependency list.

Three properties are worth asserting directly rather than trusting a
printed message:

1. ``fastembed`` and ``sqlite-vec`` are core ``dependencies``, not
   extras. A default ``pip install feral-ai`` that lands on the SHA-256
   hash provider is not "memory search, slightly worse", it is not
   semantic search at all.
2. The install shells out with ``sys.executable -m pip``, so the wizard
   installs into the interpreter that will import the packages, and not
   into whatever a ``pip`` on ``PATH`` happens to point at.
3. The step verifies retrieval by *behaviour*. A successful import is
   not evidence: ``_fastembed_embed`` silently returns hash vectors when
   the model cannot be constructed, so only an actual paraphrase-beats-
   unrelated comparison can tell the two apart.

Nothing here downloads wheels. The installer is driven through the
injected ``runner``; the one test that really embeds is skipped when
fastembed is not importable.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE))

from cli.setup.state import WizardState  # noqa: E402
from cli.setup.steps import memory as step  # noqa: E402


@pytest.fixture
def state(tmp_path):
    return WizardState(home=tmp_path)


class Console:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, text="", *args, **kwargs):
        self.lines.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def console(monkeypatch):
    fake = Console()
    monkeypatch.setattr(step, "get_console", lambda: fake)
    return fake


class _FakeProvider:
    """Stands in for ``EmbeddingProvider`` without touching a model."""

    def __init__(self, name="fastembed", dim=384, mode="auto", vectors=None):
        self.provider_name = name
        self.dimension = dim
        self.provider_mode = mode
        self._vectors = vectors or {}
        self.embedded: list[str] = []

    async def embed(self, text):
        self.embedded.append(text)
        return self._vectors[text]


class TestCoreDependency:
    """Task 1: the stack installs by default, not behind an extra."""

    @staticmethod
    def _project():
        return tomllib.loads((CORE / "pyproject.toml").read_text())["project"]

    def test_fastembed_is_a_core_dependency(self):
        deps = self._project()["dependencies"]
        assert any(d.startswith("fastembed") for d in deps), (
            "fastembed must be a core dependency. Without it "
            "EmbeddingProvider selects the SHA-256 hash fallback and "
            "natural-language memory search returns nothing."
        )

    def test_sqlite_vec_is_a_core_dependency(self):
        deps = self._project()["dependencies"]
        assert any(d.startswith("sqlite-vec") for d in deps)

    def test_the_embeddings_extra_still_resolves(self):
        """Install guides and the `feral doctor` hint both name it."""
        extras = self._project()["optional-dependencies"]
        assert "embeddings" in extras
        assert any(d.startswith("fastembed") for d in extras["embeddings"])
        assert any(d.startswith("sqlite-vec") for d in extras["embeddings"])

    def test_the_vec_extra_still_resolves(self):
        extras = self._project()["optional-dependencies"]
        assert any(d.startswith("sqlite-vec") for d in extras["vec"])

    def test_no_torch_rides_in(self):
        """The whole argument for fastembed over sentence-transformers."""
        deps = self._project()["dependencies"]
        assert not any("torch" in d for d in deps)


class TestInstallerArgv:
    def test_installs_into_the_running_interpreter(self):
        argv = step._pip_argv(step.EMBEDDING_PACKAGES)
        assert argv[:4] == [sys.executable, "-m", "pip", "install"]

    def test_both_packages_are_pinned_to_the_pyproject_ranges(self):
        argv = step._pip_argv(step.EMBEDDING_PACKAGES)
        assert "fastembed>=0.8,<0.9" in argv
        assert "sqlite-vec>=0.1.1,<1.0" in argv

    def test_a_failed_install_reports_the_last_line_not_a_traceback(self):
        def runner(argv):
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="ERROR: No matching distribution"
            )

        ok, message = step.install_embedding_stack(runner=runner)
        assert ok is False
        assert "No matching distribution" in message

    def test_an_exploding_runner_is_reported_not_raised(self):
        def runner(argv):
            raise subprocess.TimeoutExpired(argv, step.INSTALL_TIMEOUT_SECONDS)

        ok, message = step.install_embedding_stack(runner=runner)
        assert ok is False
        assert "TimeoutExpired" in message

    def test_success_invalidates_the_import_caches(self, monkeypatch):
        """Otherwise the provider re-detect still reports "hash"."""
        called = []
        monkeypatch.setattr(
            step.importlib, "invalidate_caches", lambda: called.append(True)
        )

        def runner(argv):
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        ok, _ = step.install_embedding_stack(runner=runner)
        assert ok is True
        assert called == [True]


class TestReporting:
    @pytest.mark.asyncio
    async def test_a_local_provider_is_named_with_its_dimension(
        self, console, state, monkeypatch
    ):
        import numpy as np

        vectors = {
            step._ANCHOR: np.array([1.0, 0.0], dtype=np.float32),
            step._PARAPHRASE: np.array([0.9, 0.1], dtype=np.float32),
            step._UNRELATED: np.array([0.0, 1.0], dtype=np.float32),
        }
        from memory import embeddings as embeddings_mod

        monkeypatch.setattr(
            step,
            "_new_provider",
            lambda: (_FakeProvider(vectors=vectors), embeddings_mod),
        )
        await step.run(state)

        assert "fastembed (384d, local and free)" in console.text
        assert "Semantic search verified" in console.text

    @pytest.mark.asyncio
    async def test_a_failed_paraphrase_check_says_so_plainly(
        self, console, state, monkeypatch
    ):
        """The hash provider's failure mode, reproduced deterministically.

        Vectors that make the unrelated sentence win must produce a
        FAILED line, never a tick.
        """
        import numpy as np

        vectors = {
            step._ANCHOR: np.array([1.0, 0.0], dtype=np.float32),
            step._PARAPHRASE: np.array([0.0, 1.0], dtype=np.float32),
            step._UNRELATED: np.array([0.95, 0.05], dtype=np.float32),
        }
        from memory import embeddings as embeddings_mod

        monkeypatch.setattr(
            step,
            "_new_provider",
            lambda: (_FakeProvider(vectors=vectors), embeddings_mod),
        )
        await step.run(state)

        assert "Semantic check FAILED" in console.text
        assert "Semantic search verified" not in console.text

    @pytest.mark.asyncio
    async def test_the_hash_row_matches_the_doctor_wording(
        self, console, state, monkeypatch
    ):
        from memory import embeddings as embeddings_mod

        monkeypatch.setattr(
            step,
            "_new_provider",
            lambda: (_FakeProvider(name="hash", mode="hash"), embeddings_mod),
        )
        await step.run(state)

        assert "hash fallback" in console.text
        assert "keyword-only and NOT semantic" in console.text

    @pytest.mark.asyncio
    async def test_an_explicit_hash_choice_is_not_overruled(
        self, console, state, monkeypatch
    ):
        """FERAL_EMBED_PROVIDER=hash is an operator decision."""
        from memory import embeddings as embeddings_mod

        installs = []
        monkeypatch.setattr(
            step,
            "install_embedding_stack",
            lambda *a, **kw: installs.append(a) or (True, "installed"),
        )
        monkeypatch.setattr(
            step,
            "_new_provider",
            lambda: (_FakeProvider(name="hash", mode="hash"), embeddings_mod),
        )
        await step.run(state)

        assert installs == []
        assert "explicit choice" in console.text

    @pytest.mark.asyncio
    async def test_openai_is_never_warmed_without_being_asked(
        self, console, state, monkeypatch
    ):
        """Each warm-up embed is a billed call on this provider."""
        from memory import embeddings as embeddings_mod

        provider = _FakeProvider(name="openai", dim=1536)
        monkeypatch.setattr(
            step, "_new_provider", lambda: (provider, embeddings_mod)
        )
        await step.run(state)

        assert provider.embedded == []
        assert "billed per call" in console.text

    def test_the_install_hint_survives_rich_markup(self):
        """``[embeddings]`` reads as a style tag and vanishes silently,
        leaving ``pip install 'feral-ai'``, a command that installs the
        wrong thing."""
        from rich.console import Console as RichConsole

        rich = RichConsole(record=True, width=200)
        rich.print(step._INSTALL_HINT_RICH)
        assert "feral-ai[embeddings]" in rich.export_text()
        assert "feral-ai[embeddings]" in step._INSTALL_HINT_PLAIN


class TestSqliteVecHonesty:
    @pytest.mark.asyncio
    async def test_an_unloadable_extension_is_never_reported_as_working(
        self, console, state, monkeypatch
    ):
        """Package installed + extension not loadable is the normal
        macOS/pyenv state, and must read as a caveat, not a success."""
        from memory import embeddings as embeddings_mod

        monkeypatch.setattr(
            step, "_new_provider", lambda: (_FakeProvider(name="hash"), embeddings_mod)
        )
        monkeypatch.setattr(step, "_offer_install", _leave_alone)
        pytest.importorskip("sqlite_vec")
        monkeypatch.setattr(embeddings_mod, "sqlite_vec_available", lambda: False)

        await step.run(state)

        assert "extension does NOT load" in console.text
        assert "extension loads" not in console.text
        assert "enable-loadable-sqlite-extensions" in console.text

    @pytest.mark.asyncio
    async def test_a_loadable_extension_is_reported_as_active(
        self, console, state, monkeypatch
    ):
        from memory import embeddings as embeddings_mod

        monkeypatch.setattr(
            step, "_new_provider", lambda: (_FakeProvider(name="hash"), embeddings_mod)
        )
        monkeypatch.setattr(step, "_offer_install", _leave_alone)
        pytest.importorskip("sqlite_vec")
        monkeypatch.setattr(embeddings_mod, "sqlite_vec_available", lambda: True)

        await step.run(state)

        assert "extension loads" in console.text


async def _leave_alone(console, provider, embeddings_mod):
    return provider, embeddings_mod


class TestRealModel:
    """The one test that actually embeds. Skipped without fastembed."""

    @pytest.mark.asyncio
    async def test_a_paraphrase_with_no_shared_words_outscores_noise(self):
        pytest.importorskip("fastembed")
        from memory.embeddings import EmbeddingProvider, cosine_similarity

        provider = EmbeddingProvider()
        if provider.provider_name != "fastembed":
            pytest.skip("fastembed is installed but not the selected provider")

        anchor = await provider.embed(step._ANCHOR)
        near = cosine_similarity(anchor, await provider.embed(step._PARAPHRASE))
        far = cosine_similarity(anchor, await provider.embed(step._UNRELATED))

        assert near > far, (
            f"paraphrase {near:.3f} did not beat unrelated {far:.3f}; the "
            f"model loaded but is not producing meaningful vectors"
        )


class TestWizardWiring:
    def test_the_step_is_in_the_flow_with_a_title(self):
        import inspect

        import cli.setup as setup_pkg
        from cli.setup.state_machine import _STEP_TITLES

        source = inspect.getsource(setup_pkg._run_async)
        assert '("memory"' in source
        assert "memory" in _STEP_TITLES


def test_no_em_dashes_in_the_step():
    """House rule, checked on raw bytes and on the decoded text."""
    path = CORE / "cli" / "setup" / "steps" / "memory.py"
    raw = path.read_bytes()
    assert "—".encode() not in raw
    assert "—" not in path.read_text()
