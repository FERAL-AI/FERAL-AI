"""Gate: every key in ``DEFAULT_SETTINGS`` must be read by something.

The recurring defect this closes
================================
FERAL has shipped the same bug four separate times: a settings key is
written by an operator surface (the setup wizard, the WebUI Settings
page, the phone Settings panel), persisted to ``settings.json``, and
then read by nothing. The operator sees a switch, flips it, the value
survives a restart, and the runtime ignores it forever.

Confirmed instances:

* ``vision.provider`` / ``vision.model`` were stored and ignored, so an
  operator who picked free local Ollama vision was still billed for
  every frame on the shared paid chat model.
* ``voice.chained.*`` was written by the phone UI and read by nothing,
  so a Groq Whisper plus ElevenLabs pick still ran Deepgram.
* ``security.autonomy_mode`` was written by the wizard and read by
  nothing, so picking "strict" silently ran "hybrid".
* ``audio.realtime_providers`` is written by the WebUI Settings page and
  read by nothing (see ``UNREAD_KEY_ALLOWLIST``).

Each was found by hand, months apart. This module turns that audit into
a test so the fifth instance fails CI instead of shipping.

How "read" is decided
=====================
Static analysis over ``feral-core`` Python sources. A key counts as read
when either of these holds:

1. **Direct read.** Some non-test module under ``feral-core`` accesses
   the leaf name in a read-shaped position (``.get("leaf")``,
   ``block["leaf"]``, ``helper("leaf", default)``,
   ``get_setting("section", "leaf")``) with the key's parent path
   segments present nearby. Write-shaped positions are scrubbed out
   first, because a key that is only ever *written* is exactly the bug.

2. **Env-var read.** ``ConfigLoader.export_as_env`` turns some settings
   into environment variables, and several subsystems resolve their
   config ONLY from those env vars (``FERAL_VISION_MAX_FRAME_KB`` is
   read by ``api/state.py``, never as ``vision.max_frame_kb``). Such a
   key is genuinely read and must not be flagged. The path-to-env-var
   map is not hardcoded here: it is *probed* by mutating one leaf at a
   time and diffing the ``export_as_env()`` output, so it cannot drift
   away from the loader.

Deliberate scoping decisions:

* Readers must live in ``feral-core``. A JS client reading a key back
  purely to render its own toggle is not a consumer, it is the write
  path round-tripping. ``audio.realtime_providers`` is read that way in
  ``feral-client-v2/src/pages/Settings.jsx`` and is still dead.
* Tests are not readers. A key whose only reader is the test pinning it
  is still dead in production.
* ``config/loader.py`` IS a reader (``brain_id``, ``access_remote_url``
  and friends resolve keys there), but its ``DEFAULT_SETTINGS`` literal
  is masked out so a key cannot vouch for itself.

The analysis is a heuristic and errs toward calling things read, so a
flagged key is worth believing. If it flags a real reader the fix is to
widen ``_read_patterns``, not to bury the key in the allowlist.
"""

import copy
import inspect
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.loader import DEFAULT_SETTINGS, ConfigLoader  # noqa: E402

CORE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = CORE_ROOT.parent
LOADER_PATH = CORE_ROOT / "config" / "loader.py"

# Build artifacts, vendored trees and the test tree itself. ``webui`` and
# ``webui_v2`` hold minified client bundles: a match in there is a build
# of a JS file, never a runtime reader.
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", ".pytest_cache",
    "tests", "webui", "webui_v2", "docs",
}

# How far from a read-shaped hit the parent path segments may sit before
# the hit stops counting as "this key". Wide enough for a docstring plus
# a helper definition between the two, narrow enough that an unrelated
# ``.get("enabled")`` in a 3000-line module does not vouch for
# ``memory.decay.enabled``.
_CONTEXT_WINDOW = 400

# Write-shaped constructs, blanked before the read scan. Replaced with
# spaces rather than deleted so byte offsets (and therefore the context
# window) stay aligned with the original source.
_WRITE_CALL = re.compile(
    r"\b(?:update_settings|set_setting|save_user_settings|setdefault)\s*\([^)]*\)",
    re.S,
)
_SUBSCRIPT_ASSIGN = re.compile(r"\[\s*['\"][A-Za-z_][A-Za-z0-9_]*['\"]\s*\]\s*=(?!=)")
_DICT_LITERAL_KEY = re.compile(r"['\"][A-Za-z_][A-Za-z0-9_]*['\"]\s*:")


# ---------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------
#
# An entry here says "this key has no reader and that is intentional".
# It is NOT a place to park a key you did not have time to wire up
# without saying so. Three separate tests keep it honest:
#
#   * every entry must carry a reason long enough to be a real sentence
#     (``test_allowlist_entries_are_justified``),
#   * the reason may not be a filler phrase like "TODO" or "for now",
#   * an entry whose key HAS acquired a reader, or which no longer
#     exists in DEFAULT_SETTINGS, fails as stale
#     (``test_allowlist_has_no_stale_entries``).
#
# So the list cannot grow silently and cannot rot quietly.
UNREAD_KEY_ALLOWLIST: dict[str, str] = {
    "version": (
        "Schema stamp for the settings file itself, not a behaviour "
        "switch. No operator surface writes it and no runtime path "
        "branches on it, so the read-nothing defect class does not "
        "apply. It is persisted so a future settings migration can tell "
        "which layout a file on disk was written with, and it is pinned "
        "by TestConfigDiscovery::test_default_settings_returned_when_no_files "
        "in tests/test_config.py."
    ),
    "audio.realtime_providers": (
        "Genuinely unread, kept deliberately pending a wiring change "
        "that lands outside this lane's ownership. voice/router.py "
        "resolves the realtime provider from the scalar "
        "audio.realtime_primary via _preferred_realtime_provider(); the "
        "ordered list here is written by feral-client-v2 "
        "src/pages/Settings.jsx and consumed by nobody. Removing the "
        "default would leave that page's Realtime group with no "
        "preselected providers, so the key stays until router.py learns "
        "to walk the list. Remove this entry the moment it does."
    ),
}

_ALLOWLIST_FILLER = (
    "todo", "fixme", "tbd", "for now", "later", "unknown", "not sure",
    "temporary", "wip", "xxx",
)


# ---------------------------------------------------------------------
# Walking DEFAULT_SETTINGS
# ---------------------------------------------------------------------

def walk_setting_paths(tree: dict, prefix: str = "") -> list[str]:
    """Dotted paths for every leaf in ``tree``, nested to any depth.

    A non-empty dict is a namespace and is recursed into. Everything
    else, including an EMPTY dict, is a leaf: an empty dict default is
    a real key an operator can populate, so it needs a reader like any
    other.
    """
    paths: list[str] = []
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            paths.extend(walk_setting_paths(value, path))
        else:
            paths.append(path)
    return paths


def _set_path(tree: dict, path: str, value) -> None:
    parts = path.split(".")
    node = tree
    for segment in parts[:-1]:
        node = node[segment]
    node[parts[-1]] = value


# ---------------------------------------------------------------------
# Probing export_as_env
# ---------------------------------------------------------------------

def _enum_sentinels() -> list[str]:
    """Extra string sentinels harvested from ``export_as_env``'s source.

    Some exports validate before emitting: ``security.autonomy_mode``
    only reaches ``FERAL_AUTONOMY`` when it is one of strict / hybrid /
    loose, and anything else is rewritten to the default. A nonsense
    sentinel is therefore invisible to a value diff, and the key would
    look unexported even though it is the flagship example of this whole
    bug class being fixed.

    Any such whitelist has to name its accepted values as literals
    inside ``export_as_env``, so scraping that function's own source for
    lowercase literals yields the candidates without hardcoding a list
    here that would drift.
    """
    source = inspect.getsource(ConfigLoader.export_as_env)
    return sorted(set(re.findall(r"['\"]([a-z][a-z0-9_.\-]{0,30})['\"]", source)))


def probe_env_export_map() -> dict[str, frozenset[str]]:
    """Map ``settings path -> env vars its value reaches``.

    Mutates one leaf at a time to a sentinel, re-runs
    ``export_as_env()``, and records which env vars changed. Probing
    instead of hardcoding means the map tracks the loader automatically:
    a new export shows up here without anyone editing this test, and a
    deleted export stops vouching for its key immediately.
    """
    def _export(tree: dict) -> dict[str, str]:
        loader = ConfigLoader()
        loader._merged = tree
        loader._credentials = {}
        return loader.export_as_env()

    baseline = _export(copy.deepcopy(DEFAULT_SETTINGS))
    enum_candidates = _enum_sentinels()
    mapping: dict[str, frozenset[str]] = {}

    for path in walk_setting_paths(DEFAULT_SETTINGS):
        default = DEFAULT_SETTINGS
        for segment in path.split("."):
            default = default[segment]

        # A sentinel must differ from the default or the diff sees
        # nothing. Strings get the validated-enum candidates appended as
        # fallbacks, tried only until one of them moves an env var.
        if isinstance(default, bool):
            sentinels = [not default]
        elif isinstance(default, str):
            sentinels = ["zzprobesentinelzz"] + [
                c for c in enum_candidates if c != default
            ]
        elif isinstance(default, (int, float)):
            sentinels = [987654321]
        elif isinstance(default, list):
            sentinels = [["zzprobesentinelzz"]]
        else:
            # None and empty dict have no meaningful export shape.
            continue

        for sentinel in sentinels:
            tree = copy.deepcopy(DEFAULT_SETTINGS)
            _set_path(tree, path, sentinel)
            try:
                mutated = _export(tree)
            except Exception:
                continue
            changed = {
                name for name in set(baseline) | set(mutated)
                if baseline.get(name) != mutated.get(name)
            }
            if changed:
                mapping[path] = frozenset(changed)
                break

    return mapping


# ---------------------------------------------------------------------
# Source corpus
# ---------------------------------------------------------------------

def _scrub_writes(text: str) -> str:
    """Blank out write-shaped constructs, preserving offsets."""
    def _blank(match: re.Match) -> str:
        return " " * len(match.group(0))

    text = _WRITE_CALL.sub(_blank, text)
    text = _SUBSCRIPT_ASSIGN.sub(_blank, text)
    text = _DICT_LITERAL_KEY.sub(_blank, text)
    return text


def _mask_default_settings(source: str) -> str:
    """Blank the ``DEFAULT_SETTINGS`` literal so it cannot self-vouch."""
    start = source.index("DEFAULT_SETTINGS = {")
    end = source.index("\ndef feral_home", start)
    return source[:start] + " " * (end - start) + source[end:]


def load_source_corpus() -> dict[str, str]:
    """``relative path -> scrubbed source`` for every runtime module."""
    corpus: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(CORE_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            if not filename.endswith(".py") or filename.startswith("test_"):
                continue
            full = Path(dirpath) / filename
            try:
                source = full.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if full == LOADER_PATH:
                source = _mask_default_settings(source)
            corpus[str(full.relative_to(REPO_ROOT))] = _scrub_writes(source)
    return corpus


# ---------------------------------------------------------------------
# Reader detection
# ---------------------------------------------------------------------

SETTINGS_ROOT = r"(?:settings|_merged|_settings|cfg|config|conf)\w*"


def _read_patterns(leaf: str, *, top_level: bool) -> list[re.Pattern]:
    """Read-shaped ways this repo pulls a settings leaf out of a dict.

    Attribute access (``cfg.leaf``) is deliberately absent: settings are
    plain dicts everywhere in feral-core, so ``.leaf`` only ever matches
    an unrelated object attribute and manufactures false readers.

    A ``top_level`` leaf sits directly on the settings root and so has no
    parent segment to disambiguate it. Generic names there (``version``)
    otherwise collect a hit from every unrelated manifest or payload in
    the tree, so those must be read off an identifier that names itself
    as the settings root.
    """
    escaped = re.escape(leaf)
    if top_level:
        return [
            re.compile(SETTINGS_ROOT + r"\s*\.get\(\s*['\"]" + escaped + r"['\"]"),
            re.compile(SETTINGS_ROOT + r"\s*\[\s*['\"]" + escaped + r"['\"]\s*\]"),
        ]
    return [
        # block.get("leaf") / block.get("leaf", default)
        re.compile(r"\.get\(\s*['\"]" + escaped + r"['\"]"),
        # helper("leaf", default) -- e.g. VadConfig.from_settings's _num
        re.compile(r"\w\(\s*['\"]" + escaped + r"['\"]\s*[,)]"),
        # get_setting("section", "leaf") / cfg.get("section", "leaf")
        re.compile(r"\w\(\s*['\"][a-z_]+['\"]\s*,\s*['\"]" + escaped + r"['\"]\s*[,)]"),
        # block["leaf"] -- assignments were scrubbed, so this is a read
        re.compile(r"\[\s*['\"]" + escaped + r"['\"]\s*\]"),
    ]


def _context_patterns(segments: list[str]) -> list[re.Pattern]:
    """Tokens that must appear near a hit for it to be about this key.

    For ``memory.decay.forget_threshold`` the parents are ``memory`` and
    ``decay``; a bare ``.get("enabled")`` somewhere else in the tree does
    not get to vouch for ``memory.decay.enabled``. Matching is prefix-
    anchored and case-insensitive so ``decay`` finds ``DecayConfig`` and
    ``vad`` finds ``VadConfig``.

    A top-level scalar has no parent segments; its disambiguation lives
    in the root-anchored read patterns instead, so it needs no window.
    """
    return [re.compile(r"\b" + re.escape(s), re.I) for s in segments]


def find_readers(path: str, corpus: dict[str, str]) -> list[str]:
    """Files that read ``path`` directly (not via its exported env var)."""
    segments = path.split(".")
    leaf = segments[-1]
    patterns = _read_patterns(leaf, top_level=len(segments) == 1)
    context = _context_patterns(segments[:-1])
    # A literal "memory.decay.enabled" in a comment or log line is
    # unambiguous on its own. A single-segment path is just a bare word,
    # which would match everything, so it gets no such shortcut.
    dotted = re.compile(re.escape(path)) if len(segments) > 1 else None

    readers: list[str] = []
    for filename, text in corpus.items():
        if dotted is not None and dotted.search(text):
            readers.append(filename)
            continue
        found = False
        for pattern in patterns:
            for match in pattern.finditer(text):
                lo = max(0, match.start() - _CONTEXT_WINDOW)
                window = filename + "\n" + text[lo:match.end() + _CONTEXT_WINDOW]
                if all(c.search(window) for c in context):
                    found = True
                    break
            if found:
                break
        if found:
            readers.append(filename)
    return readers


def find_env_readers(path: str, env_map, corpus: dict[str, str]) -> list[str]:
    """Files reading an env var that carries ``path``'s value."""
    loader_rel = str(LOADER_PATH.relative_to(REPO_ROOT))
    hits: list[str] = []
    for env_name in env_map.get(path, ()):
        needle = re.compile(r"\b" + re.escape(env_name) + r"\b")
        for filename, text in corpus.items():
            if filename == loader_rel:
                continue
            if needle.search(text):
                hits.append(f"{filename} (via {env_name})")
    return hits


def audit_unread_keys() -> dict[str, str]:
    """``path -> "" `` for every DEFAULT_SETTINGS leaf with no reader."""
    corpus = load_source_corpus()
    env_map = probe_env_export_map()
    unread: dict[str, str] = {}
    for path in walk_setting_paths(DEFAULT_SETTINGS):
        if find_readers(path, corpus) or find_env_readers(path, env_map, corpus):
            continue
        unread[path] = ""
    return unread


@pytest.fixture(scope="module")
def unread_keys() -> dict[str, str]:
    return audit_unread_keys()


# ---------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------

def test_every_default_setting_has_a_reader(unread_keys):
    """No key may be written to disk and then ignored by the runtime."""
    offenders = sorted(set(unread_keys) - set(UNREAD_KEY_ALLOWLIST))
    assert not offenders, (
        "These DEFAULT_SETTINGS keys are read by nothing in feral-core, "
        "directly or via an env var exported by export_as_env():\n  "
        + "\n  ".join(offenders)
        + "\n\nAn operator can set each of them and the runtime will "
        "ignore the choice. Either wire a reader, delete the key, or add "
        "it to UNREAD_KEY_ALLOWLIST with a real reason."
    )


def test_allowlist_entries_are_justified():
    """A bare allowlist would defeat the point, so reasons are enforced."""
    for path, reason in UNREAD_KEY_ALLOWLIST.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 60, (
            f"UNREAD_KEY_ALLOWLIST[{path!r}] needs a real explanation of "
            f"why nothing reads this key, not {reason!r}."
        )
        lowered = reason.lower()
        for filler in _ALLOWLIST_FILLER:
            assert filler not in lowered, (
                f"UNREAD_KEY_ALLOWLIST[{path!r}] reads as a placeholder "
                f"({filler!r}). Say why the key legitimately has no "
                f"reader, or wire it up."
            )


def test_allowlist_has_no_stale_entries(unread_keys):
    """An entry that stopped being true must be removed, not left to rot."""
    known = set(walk_setting_paths(DEFAULT_SETTINGS))
    for path in UNREAD_KEY_ALLOWLIST:
        assert path in known, (
            f"UNREAD_KEY_ALLOWLIST[{path!r}] names a key that is no longer "
            f"in DEFAULT_SETTINGS. Drop the entry."
        )
        assert path in unread_keys, (
            f"UNREAD_KEY_ALLOWLIST[{path!r}] now HAS a reader. Drop the "
            f"entry so the gate protects the key again."
        )


# ---------------------------------------------------------------------
# Self-tests: prove the gate can actually fail
# ---------------------------------------------------------------------

def test_analyzer_flags_a_synthetic_dead_key():
    """A key nothing could possibly read must come back unread.

    Without this, a bug that made ``find_readers`` return a hit for
    everything would turn the gate into a no-op that still passes.
    """
    corpus = load_source_corpus()
    assert find_readers("llm.zzz_no_such_setting_zzz", corpus) == []
    assert find_readers("zzz_no_such_section_zzz.enabled", corpus) == []
    assert find_readers("zzz_no_such_top_level_key_zzz", corpus) == []


def test_analyzer_finds_a_known_direct_reader():
    corpus = load_source_corpus()
    # voice/vad.py resolves this through a _num("threshold", ...) helper,
    # which is why the patterns cannot be limited to plain .get().
    readers = find_readers("voice.vad.min_silence_ms", corpus)
    assert any("voice/vad.py" in r for r in readers), readers


def test_analyzer_finds_a_known_env_only_reader():
    """``vision.max_frame_kb`` is read ONLY as FERAL_VISION_MAX_FRAME_KB."""
    corpus = load_source_corpus()
    env_map = probe_env_export_map()
    assert env_map.get("vision.max_frame_kb") == frozenset(
        {"FERAL_VISION_MAX_FRAME_KB"}
    )
    hits = find_env_readers("vision.max_frame_kb", env_map, corpus)
    assert any("api/state.py" in h for h in hits), hits


def test_env_probe_tracks_the_loader():
    """The probe must discover exports without being told about them."""
    env_map = probe_env_export_map()
    # autonomy_mode is the canonical "wizard wrote it, only the env var
    # is read" fix; if the probe cannot see it, it cannot protect anything.
    assert "FERAL_AUTONOMY" in env_map.get("security.autonomy_mode", frozenset())
    assert "FERAL_LLM_PROVIDER" in env_map.get("llm.provider", frozenset())


def test_walk_handles_arbitrary_nesting():
    paths = walk_setting_paths(
        {"a": {"b": {"c": {"d": 1}}}, "e": 2, "f": {}, "g": {"h": []}}
    )
    assert sorted(paths) == ["a.b.c.d", "e", "f", "g.h"]
