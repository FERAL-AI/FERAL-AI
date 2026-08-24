"""Memory / embeddings preflight: prove semantic search actually works.

Why this step exists
====================
Persistent memory is the feature FERAL is installed for, and until
v2026.8.3 the piece that makes it *semantic* shipped as an opt-in extra.
A default install therefore ran on the deterministic SHA-256 ``hash``
provider, where ``feral memory search "building"`` finds a note and
``feral memory search "what am I building?"`` returns nothing. Every
query still returns a result set, so nothing in the product ever told
the user their memory search was keyword-only.

``fastembed`` and ``sqlite-vec`` are core dependencies now, so on a
fresh install this step is a confirmation rather than a repair. It
still earns its place three ways:

* **Existing installs.** Anyone who installed before v2026.8.3 keeps
  the hash provider until they upgrade. This step detects that, offers
  to install the local stack into the running interpreter, and does it.
* **The first query is not a cold start.** fastembed lazy-loads the
  model on first embed (deliberately, so ``feral serve`` boots fast).
  That is a ~0.4s cold load plus, on a genuinely fresh machine, a
  ~130MB model download. Warming it here moves both costs into setup,
  where the user is already waiting on a progress line.
* **It verifies behaviour, not imports.** ``import fastembed`` succeeding
  proves nothing about retrieval. The check below embeds three
  sentences and requires a paraphrase with zero lexical overlap to
  outscore an unrelated sentence. The hash provider fails that by
  construction, which is exactly the point.

Honest reporting of sqlite-vec
------------------------------
``pip install sqlite-vec`` succeeding does not mean the extension can
load. Loading any SQLite extension needs an interpreter built with
``--enable-loadable-sqlite-extensions``, which pyenv omits on macOS by
default: ``sqlite3.Connection`` then has no ``enable_load_extension``
and the extension can never load, no matter how it was installed. This
step reports the package and the extension separately and never claims
the second from the first.

Honest about what it costs, too. This step used to tell the user the
numpy path was "O(n) per query" and hand them a CPython rebuild.
sqlite-vec 0.1.9 builds no ANN index, so a vec0 ``MATCH`` is also a full
scan; measured on this machine at 384 dims, top-5, numpy runs 0.46ms vs
vec0's 7.08ms at 12k chunks and 3.97ms vs 56.99ms at 100k, for identical
results. The rebuild was a slowdown. What sqlite-vec actually saves is
resident memory (numpy holds ~18MB at 12k rows, ~154MB at 100k), so that
is what the instructions are now attached to.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
import time
from typing import Callable, Optional

from ..helpers import (
    confirm,
    get_console,
    _RICH_AVAILABLE,
)
from ..state import WizardState


# The packages `pip install 'feral-ai[embeddings]'` resolves to. Named
# here rather than installing the extra itself because the wizard may be
# running from a git checkout where "feral-ai" is not a resolvable
# distribution name.
EMBEDDING_PACKAGES = ("fastembed>=0.8,<0.9", "sqlite-vec>=0.1.1,<1.0")

# Wheels are ~120MB and the first resolve can be slow on a bad link.
# Long enough to finish, short enough that a wedged pip does not hold
# the wizard open forever.
INSTALL_TIMEOUT_SECONDS = 900

# Providers that embed locally, for free, with no network round-trip.
_LOCAL_PROVIDERS = ("fastembed", "sentence_transformers")

# The verification triad. `_PARAPHRASE` shares no content word with
# `_ANCHOR` ("eyewear"/"glasses", "send sound"/"stream audio"), so a
# lexical or hash-projection matcher cannot score it above `_UNRELATED`
# by accident. Both probes are drawn from FERAL's own domain so the
# sentences read as something a user would plausibly have stored.
_ANCHOR = "Theora glasses stream audio over BLE to the iPhone"
_PARAPHRASE = "how does the eyewear send sound?"
_UNRELATED = "The espresso machine needs descaling every two months"


# The manual-install hint, verbatim from the `feral doctor` row so the
# two never drift. Rich treats ``[embeddings]`` as a style tag and eats
# it silently, printing ``pip install 'feral-ai'``, a command that
# installs the wrong thing. ``\[`` is the Rich escape; the plain-console
# path must not carry it.
_INSTALL_HINT_RICH = r"pip install 'feral-ai\[embeddings]'"
_INSTALL_HINT_PLAIN = "pip install 'feral-ai[embeddings]'"


def _stdin_is_interactive() -> bool:
    """True when a human can actually answer a prompt.

    Guarded with try/except because `sys.stdin` can be replaced by an
    object with no `isatty` at all under pytest capture.
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def _install_hint() -> str:
    return _INSTALL_HINT_RICH if _RICH_AVAILABLE else _INSTALL_HINT_PLAIN


def _pip_argv(packages: tuple[str, ...]) -> list[str]:
    """The exact command line. A function so tests can assert the argv.

    ``sys.executable -m pip`` and not a bare ``pip``: the wizard must
    install into the interpreter that is going to import these modules,
    which is not necessarily the one a ``pip`` on ``PATH`` targets.
    """
    return [sys.executable, "-m", "pip", "install", *packages]


def install_embedding_stack(
    packages: tuple[str, ...] = EMBEDDING_PACKAGES,
    *,
    runner: Optional[Callable[[list[str]], subprocess.CompletedProcess]] = None,
) -> tuple[bool, str]:
    """Install the local embedding stack. Returns ``(ok, message)``.

    Mirrors ``steps.external_agents.install_opencode``: same
    ``(ok, message)`` contract, same injectable ``runner`` so tests can
    prove the command line without pulling 120MB of wheels.
    """
    argv = _pip_argv(packages)
    call = runner or (
        lambda cmd: subprocess.run(
            cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS
        )
    )
    try:
        result = call(argv)
    except Exception as exc:  # network failure, timeout, pip missing
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else f"exit {result.returncode}"
    # Freshly-installed packages are invisible to an interpreter that
    # already asked for them and got a negative answer; the finder
    # caches that. Without this the provider re-detect below would still
    # report "hash" on a successful install.
    importlib.invalidate_caches()
    return True, "installed"


def _new_provider():
    """Build a fresh ``EmbeddingProvider``, re-running detection.

    Detection runs in ``__init__`` (``_detect_provider`` calls
    ``importlib.util.find_spec`` there and then), so constructing a new
    instance is all it takes for the wizard to see a package installed
    seconds ago. The module is deliberately NOT reloaded: other modules
    already hold references to its classes, and rebinding them under a
    live process buys nothing here.
    """
    from memory import embeddings as _embeddings

    return _embeddings.EmbeddingProvider(), _embeddings


async def run(state: WizardState) -> None:
    console = get_console()

    console.print(
        "FERAL remembers things you tell it and searches that memory by "
        "meaning, not by keyword. That needs a local embedding model "
        "(BAAI/bge-small-en-v1.5, 384-dim, runs on CPU, no data leaves "
        "this machine)."
    )

    try:
        provider, embeddings_mod = _new_provider()
    except Exception as exc:
        console.print(
            f"  [yellow]Could not load the embedding provider: {exc}[/]"
            if _RICH_AVAILABLE else
            f"  Could not load the embedding provider: {exc}"
        )
        return

    _report_provider(console, provider)

    if provider.provider_name == "hash":
        provider, embeddings_mod = await _offer_install(
            console, provider, embeddings_mod
        )

    if provider.provider_name in _LOCAL_PROVIDERS:
        await _warm_and_verify(console, provider, embeddings_mod)
    elif provider.provider_name == "openai":
        # Deliberately no warm-up and no verification probe. Every embed
        # on this provider is a billed API call, and spending the
        # operator's money to prove a third-party endpoint works is not
        # this step's call to make.
        console.print(
            "  Skipping the local warm-up: this provider bills per call. "
            "Unset FERAL_EMBED_PROVIDER to use the free local model."
        )
    else:
        console.print(
            "  [yellow]Skipping the semantic check: no local embedding "
            "model is active, so there is nothing to verify.[/]"
            if _RICH_AVAILABLE else
            "  Skipping the semantic check: no local embedding model is "
            "active, so there is nothing to verify."
        )

    _report_sqlite_vec(console, embeddings_mod)


def _report_provider(console, provider) -> None:
    """One line naming the active provider, matching `feral doctor`."""
    name = provider.provider_name
    dim = provider.dimension

    if name in _LOCAL_PROVIDERS:
        console.print(
            f"  [green]✔[/] Embedding provider: {name} ({dim}d, local and free)"
            if _RICH_AVAILABLE else
            f"  ✔ Embedding provider: {name} ({dim}d, local and free)"
        )
        return

    if name == "openai":
        console.print(
            f"  Embedding provider: OpenAI text-embedding-3-small ({dim}d, "
            f"explicitly selected, billed per call)"
        )
        return

    # Same wording as the `feral doctor` row, on purpose: an operator who
    # sees both should not have to work out whether they describe the
    # same condition.
    console.print(
        f"  [yellow]![/] Embedding provider: hash fallback ({dim}d), memory "
        f"search is keyword-only and NOT semantic."
        if _RICH_AVAILABLE else
        f"  ! Embedding provider: hash fallback ({dim}d), memory search is "
        f"keyword-only and NOT semantic."
    )


async def _offer_install(console, provider, embeddings_mod):
    """Offer to install the local stack. Returns the (possibly new) pair.

    Reached on installs that predate v2026.8.3, where fastembed was an
    opt-in extra, and on any install where the packages were removed.
    """
    mode = getattr(provider, "provider_mode", "auto")
    if mode == "hash":
        # The operator asked for this explicitly. Installing over an
        # explicit choice would be the wizard overruling them.
        console.print(
            "  FERAL_EMBED_PROVIDER=hash is set, so this is your explicit "
            "choice and setup will leave it alone. Unset it to use the "
            "local model."
        )
        return provider, embeddings_mod

    console.print(
        "  Your install predates local embeddings being part of the base "
        "install. FERAL can fetch them now: ~120MB of wheels (onnxruntime "
        "and friends, no torch) plus a one-time ~130MB model download."
    )

    # Never block on stdin when nobody is there to answer.
    #
    # `feral setup` gets driven non-interactively in three real
    # situations: a scripted or piped install, CI, and any test that
    # walks the whole wizard rather than this one step. In all three,
    # `confirm` reaches for /dev/tty and dies with "reading from stdin
    # while output is captured" (this is exactly how
    # test_setup_wizard_preflights broke on the 3.12 CI leg, since that
    # test drives every step and only stubs the ones it knows about).
    #
    # Downloading ~250MB unattended would be the wrong default, so the
    # non-interactive answer is "do not install, say how", not "assume
    # yes".
    if not _stdin_is_interactive():
        console.print(
            f"  Non-interactive setup, so nothing was installed. "
            f"To add local embeddings: {_install_hint()}"
        )
        return provider, embeddings_mod

    if not confirm("  Install local embeddings now?", default=True):
        console.print(
            f"  Skipped. To do it later: {_install_hint()}  "
            f"(then `feral setup --from-step memory`)"
        )
        return provider, embeddings_mod

    console.print(f"  Running: {' '.join(_pip_argv(EMBEDDING_PACKAGES))}")
    ok, message = install_embedding_stack()
    if not ok:
        console.print(
            f"  [yellow]Install failed: {message}[/]"
            if _RICH_AVAILABLE else f"  Install failed: {message}"
        )
        console.print(f"  You can retry by hand: {_install_hint()}")
        return provider, embeddings_mod

    try:
        provider, embeddings_mod = _new_provider()
    except Exception as exc:
        console.print(
            f"  [yellow]Installed, but the provider could not be reloaded: "
            f"{exc}. Restart the shell, then `feral setup --from-step memory`.[/]"
            if _RICH_AVAILABLE else
            f"  Installed, but the provider could not be reloaded: {exc}. "
            f"Restart the shell, then `feral setup --from-step memory`."
        )
        return provider, embeddings_mod

    if provider.provider_name == "hash":
        # pip reported success and the provider still cannot see the
        # package. Say so; do not print a green tick over it.
        console.print(
            "  [yellow]pip reported success but the embedding provider is "
            "still on the hash fallback. Restart your shell, then "
            "`feral setup --from-step memory`.[/]"
            if _RICH_AVAILABLE else
            "  pip reported success but the embedding provider is still on "
            "the hash fallback. Restart your shell, then "
            "`feral setup --from-step memory`."
        )
        return provider, embeddings_mod

    _report_provider(console, provider)
    return provider, embeddings_mod


async def _warm_and_verify(console, provider, embeddings_mod) -> None:
    """Load the model once, then prove retrieval is semantic.

    The warm-up and the check are the same three embed calls: the first
    one pays the model-load cost, and all three feed the comparison. A
    paraphrase that shares no content word with the anchor must score
    above an unrelated sentence, or this prints a failure.
    """
    console.print("  Warming the model (first load downloads it if missing)…")
    started = time.monotonic()
    try:
        anchor = await provider.embed(_ANCHOR)
        warm_seconds = time.monotonic() - started
        paraphrase = await provider.embed(_PARAPHRASE)
        unrelated = await provider.embed(_UNRELATED)
    except Exception as exc:
        console.print(
            f"  [red]✘[/] Embedding failed: {exc}. Memory search will fall "
            f"back to keyword-only."
            if _RICH_AVAILABLE else
            f"  ✘ Embedding failed: {exc}. Memory search will fall back to "
            f"keyword-only."
        )
        return

    cosine = embeddings_mod.cosine_similarity
    near = cosine(anchor, paraphrase)
    far = cosine(anchor, unrelated)

    console.print(f"  Model ready in {warm_seconds:.2f}s.")
    console.print(
        f"    \"{_PARAPHRASE}\"  vs the stored note: {near:+.3f}"
    )
    console.print(
        f"    \"{_UNRELATED[:40]}…\"  vs the same note: {far:+.3f}"
    )

    if near > far:
        console.print(
            f"  [green]✔[/] Semantic search verified: the paraphrase scores "
            f"{near - far:+.3f} higher than the unrelated sentence, with no "
            f"words in common."
            if _RICH_AVAILABLE else
            f"  ✔ Semantic search verified: the paraphrase scores "
            f"{near - far:+.3f} higher than the unrelated sentence, with no "
            f"words in common."
        )
        return

    console.print(
        f"  [red]✘[/] Semantic check FAILED: the paraphrase ({near:+.3f}) did "
        f"not outscore an unrelated sentence ({far:+.3f}). The model loaded "
        f"but is not producing meaningful vectors, so treat memory search as "
        f"keyword-only until this is fixed."
        if _RICH_AVAILABLE else
        f"  ✘ Semantic check FAILED: the paraphrase ({near:+.3f}) did not "
        f"outscore an unrelated sentence ({far:+.3f}). The model loaded but "
        f"is not producing meaningful vectors, so treat memory search as "
        f"keyword-only until this is fixed."
    )


def _report_sqlite_vec(console, embeddings_mod) -> None:
    """Report the package and the extension as two separate facts."""
    installed = importlib.util.find_spec("sqlite_vec") is not None
    if not installed:
        console.print(
            "  [cyan]i[/] sqlite-vec is not installed, so vector search runs over "
            "numpy. That is the faster path at every corpus size measured; "
            "sqlite-vec keeps vectors on disk instead of in RAM, which is worth "
            "it on a large store. pip install sqlite-vec"
            if _RICH_AVAILABLE else
            "  i sqlite-vec is not installed, so vector search runs over numpy. "
            "That is the faster path at every corpus size measured; sqlite-vec "
            "keeps vectors on disk instead of in RAM, which is worth it on a "
            "large store. pip install sqlite-vec"
        )
        return

    try:
        loadable = bool(embeddings_mod.sqlite_vec_available())
    except Exception as exc:
        console.print(
            f"  [yellow]![/] sqlite-vec installed; could not test the "
            f"extension load: {exc}"
            if _RICH_AVAILABLE else
            f"  ! sqlite-vec installed; could not test the extension load: "
            f"{exc}"
        )
        return

    if loadable:
        console.print(
            "  [green]✔[/] sqlite-vec: package installed, extension loads. "
            "indexed vector search is active."
            if _RICH_AVAILABLE else
            "  ✔ sqlite-vec: package installed, extension loads. Indexed "
            "vector search is active."
        )
        return

    # The common macOS/pyenv case. Installing harder does not fix it, so do
    # not suggest that; only the interpreter can change it. Reported as a
    # fact and not a problem, because measurement says the path it leaves
    # you on is the faster one.
    console.print(
        "  [cyan]i[/] sqlite-vec: package installed, extension does NOT "
        "load. This Python was built without loadable SQLite extension "
        "support (pyenv omits it on macOS by default). Memory search runs "
        "over numpy, which is correct and measured faster than vec0 at "
        "every corpus size tested. sqlite-vec's advantage is holding "
        "vectors on disk rather than in RAM (~18MB at 12k chunks, ~154MB "
        "at 100k). If that memory matters on your store: rebuild with "
        "PYTHON_CONFIGURE_OPTS=\"--enable-loadable-sqlite-extensions\", or "
        "use a python.org / Homebrew interpreter."
        if _RICH_AVAILABLE else
        "  i sqlite-vec: package installed, extension does NOT load. This "
        "Python was built without loadable SQLite extension support (pyenv "
        "omits it on macOS by default). Memory search runs over numpy, "
        "which is correct and measured faster than vec0 at every corpus "
        "size tested. sqlite-vec's advantage is holding vectors on disk "
        "rather than in RAM (~18MB at 12k chunks, ~154MB at 100k). If that "
        "memory matters on your store: rebuild with PYTHON_CONFIGURE_OPTS="
        "\"--enable-loadable-sqlite-extensions\", or use a python.org / "
        "Homebrew interpreter."
    )


__all__ = [
    "run",
    "install_embedding_stack",
    "_pip_argv",
    "EMBEDDING_PACKAGES",
]
