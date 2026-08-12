"""F-08 — both node SDKs must name a node's key file identically.

Both SDKs persist the pairing token to ``~/.feral/node-keys/<safe>.key``, and
both derived ``<safe>`` by hand from prose in ``HUP_SPEC.md``. They landed on
different algorithms, so the same node paired through one SDK and then run
through the other silently re-paired:

    node_id             python (before)      typescript (before)
    "sensor 01"         sensor01.key         sensor_01.key
    "café"              café.key             caf_.key
    "!!!"               .key                 ___.key

Python dropped disallowed characters and its ``str.isalnum()`` is Unicode-aware,
so it kept "é" and wrote non-ASCII filenames. TypeScript replaced disallowed
characters with "_" against an ASCII-only class. And ``HUP_SPEC.md`` §4.1
documented the path as ``<node_id>.key`` with no sanitisation at all, so there
were three specifications, not two.

Both collapse distinct node ids onto one file, which is worse than a mismatch:
Python mapped every all-punctuation id and the empty id onto a hidden file
literally named ``.key``, and TypeScript mapped both "a b" and "a_b" onto
``a_b.key``.

The fixture below is shared verbatim with
``feral-nodes/ts-node-sdk/tests/keyFilename.test.ts``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from feral_node_sdk import pairing


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "spec-fixtures" / "node_key_filename.json"
)
SPEC_PATH = Path(__file__).resolve().parents[2] / "HUP_SPEC.md"
TS_MIRROR = (
    Path(__file__).resolve().parents[2] / "ts-node-sdk" / "tests" / "keyFilename.test.ts"
)

FIXTURE: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = [(c["name"], c) for c in FIXTURE["cases"]]


def _filename(node_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """The name ``_key_path`` gives this node id, with the real keys dir moved.

    ``_key_path`` mkdirs, so the fixture must not write into the developer's own
    ``~/.feral/node-keys``.
    """
    monkeypatch.setattr(pairing, "KEYS_DIR", tmp_path / "node-keys")
    return pairing._key_path(node_id).name


@pytest.mark.parametrize("name,case", CASES, ids=[n for n, _ in CASES])
def test_filename_matches_the_shared_fixture(
    name: str, case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    assert _filename(case["node_id"], tmp_path, monkeypatch) == case["filename"], (
        f"{name}: {case['why']}"
    )


def test_every_fixture_node_id_gets_its_own_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The point of the algorithm: distinct node ids, distinct key files.

    Both old rules failed this. Python mapped "", "!!!" and "😀" all onto
    ".key"; TypeScript mapped "a b" and "a_b" both onto "a_b.key".
    """
    seen: dict[str, str] = {}
    for _, case in CASES:
        node_id = case["node_id"]
        name = _filename(node_id, tmp_path, monkeypatch)
        assert name not in seen, (
            f"{node_id!r} and {seen[name]!r} both resolve to {name!r}"
        )
        seen[name] = node_id


@pytest.mark.parametrize("name,case", CASES, ids=[n for n, _ in CASES])
def test_key_file_stays_inside_the_keys_directory(
    name: str, case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No node id may escape ~/.feral/node-keys. Neither old rule could, and
    the hash suffix must not introduce a way to."""
    keys_dir = tmp_path / "node-keys"
    monkeypatch.setattr(pairing, "KEYS_DIR", keys_dir)
    resolved = pairing._key_path(case["node_id"]).resolve()
    assert resolved.parent == keys_dir.resolve(), f"{name}: escaped to {resolved}"


def test_already_legal_node_ids_keep_the_filename_they_have_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Compatibility guard.

    Any node id the brain accepts (`^[A-Za-z0-9._:-]{1,128}$`) must keep the
    exact filename both SDKs already write, or this fix would silently unpair
    working hardware. The fixture records the pre-fix names, so this compares
    against measurement rather than against a restatement of the new rule.
    """
    unchanged = [
        c for _, c in CASES
        if c["before_python"] == c["before_ts"] == c["filename"]
    ]
    assert len(unchanged) >= 4, "fixture lost its compatibility cases"
    for case in unchanged:
        assert _filename(case["node_id"], tmp_path, monkeypatch) == case["before_python"]


def test_save_and_load_round_trip_through_the_canonical_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The name is only useful if both halves of the SDK use it."""
    monkeypatch.setattr(pairing, "KEYS_DIR", tmp_path / "node-keys")
    written = pairing.save_key("sensor 01", "tok-abc")
    assert written.name == "sensor_01-46977d17.key"
    assert pairing.load_key("sensor 01") == "tok-abc"
    # The lookalike id must not read the neighbour's token.
    assert pairing.load_key("sensor_01") is None


def test_hup_spec_documents_the_algorithm():
    """The done-when says one algorithm, specified in HUP_SPEC.md.

    Until this passes, the spec says `<node_id>.key` verbatim, which is a third
    algorithm neither SDK implements and the one a new SDK author would copy.
    """
    spec = SPEC_PATH.read_text(encoding="utf-8")
    assert "[A-Za-z0-9._:-]" in spec, "HUP_SPEC.md does not state the allowed class"
    assert "sha256" in spec.lower(), "HUP_SPEC.md does not state the disambiguator"
    assert "node_key_filename.json" in spec, (
        "HUP_SPEC.md does not point at the shared fixture table"
    )


def test_typescript_mirror_asserts_the_same_fixture():
    """A Python-only assertion is what let these two drift apart."""
    assert TS_MIRROR.exists(), f"missing TypeScript mirror test at {TS_MIRROR}"
    text = TS_MIRROR.read_text(encoding="utf-8")
    assert "node_key_filename.json" in text, (
        "the TS test must read the shared fixture, not a copy of the numbers"
    )
