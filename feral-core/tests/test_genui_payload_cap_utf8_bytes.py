"""F-07 — the AppMessage payload cap must mean the same thing on both sides.

``MAX_PAYLOAD_BYTES`` is 64 KiB in ``genui/app_message_schema.py`` and in
``feral-client-v2/src/pages/AppSurface.types.ts``. Same name, same number, two
different quantities:

* Python measured ``json.dumps(v)`` with the stdlib defaults, which are
  ``ensure_ascii=True`` (every non-ASCII character becomes a six-byte
  ``\\uXXXX`` escape, twelve for an astral-plane character) and
  ``separators=(', ', ': ')`` (two extra bytes per key).
* JavaScript measured ``JSON.stringify(payload).length``, which is UTF-16 code
  units of a compact encoding: one unit per BMP character, two per emoji, and
  no separator padding at all.

Measured before the fix, with ``{"a": "中" * 11000}``: 66009 to Python,
which refused it, and 11008 to JavaScript, which allowed it. The browser guard
is the one an attacker controls, so it was also the loose one: 30000 CJK
characters is 90008 bytes of payload and only 30002 UTF-16 units.

Both sides now measure UTF-8 bytes of the compact JSON encoding, which is the
quantity the constant is named after. The fixture below is shared verbatim with
``feral-client-v2/src/__tests__/pages/AppSurface.payloadCap.test.js`` so the two
halves cannot drift apart again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from genui import app_message_schema
from genui.app_message_schema import MAX_PAYLOAD_BYTES, validate_app_message


def payload_size_bytes(payload: dict[str, Any]) -> int:
    """Call the module's measurement, or fail saying it is not observable.

    Resolved at call time rather than imported at module scope on purpose: an
    ImportError here would be a collection error, and a collection error proves
    only that a symbol is missing. The accept/reject tests below have to fail on
    the payload sizes themselves for this file to be evidence of anything.
    """
    fn = getattr(app_message_schema, "payload_size_bytes", None)
    if fn is None:
        pytest.fail(
            "genui.app_message_schema exposes no payload_size_bytes(), so the "
            "quantity the cap measures cannot be asserted against the browser "
            "guard's. That is how the two ended up 6x apart."
        )
    return fn(payload)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "app_message_payload_sizes.json"

# The TypeScript mirror reads this same file. If you move it, move it there too.
TS_MIRROR = (
    Path(__file__).resolve().parents[2]
    / "feral-client-v2"
    / "src"
    / "__tests__"
    / "pages"
    / "AppSurface.payloadCap.test.js"
)


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _build(spec: dict[str, Any]) -> dict[str, Any]:
    """Materialise a fixture payload. Mirrored line for line in the TS test."""
    kind = spec["kind"]
    if kind == "literal":
        return spec["value"]
    if kind == "repeat":
        return {spec["key"]: spec["unit"] * spec["count"]}
    if kind == "int_keys":
        return {f"{spec['prefix']}{i}": i for i in range(spec["count"])}
    raise AssertionError(f"unknown fixture build kind: {kind!r}")


FIXTURE = _load_fixture()
CASES = [(c["name"], c) for c in FIXTURE["cases"]]


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "submit_form",
        "payload": payload,
        "message_id": "m-1",
        "signed_with_key_id": "k-1",
    }


def test_fixture_cap_matches_the_constant():
    """The fixture's own cap must be the module's, or every number below lies."""
    assert FIXTURE["max_payload_bytes"] == MAX_PAYLOAD_BYTES


@pytest.mark.parametrize("name,case", CASES, ids=[n for n, _ in CASES])
def test_payload_size_is_utf8_bytes_of_compact_json(name: str, case: dict[str, Any]):
    """Every fixture payload measures exactly the shared expected byte count."""
    payload = _build(case["build"])
    assert payload_size_bytes(payload) == case["expected_bytes"], (
        f"{name}: {case['why']}"
    )


@pytest.mark.parametrize("name,case", CASES, ids=[n for n, _ in CASES])
def test_accept_reject_matches_the_shared_fixture(name: str, case: dict[str, Any]):
    """The accept/reject verdict must match the one the browser guard reaches."""
    payload = _build(case["build"])
    accepted = validate_app_message(_envelope(payload)) is not None
    assert accepted is case["expected_accepted"], f"{name}: {case['why']}"


def test_ensure_ascii_inflation_no_longer_refuses_a_legal_payload():
    """The audit's case, stated as behaviour rather than as a byte count.

    11000 CJK characters is 33008 bytes, half the cap. Before the fix Python
    counted the \\uXXXX escapes and made it 66009, so the brain refused a
    payload the browser had already accepted.
    """
    payload = {"a": "中" * 11000}
    assert payload_size_bytes(payload) == 33008
    assert validate_app_message(_envelope(payload)) is not None


def test_separator_padding_no_longer_refuses_a_legal_ascii_payload():
    """Pure ASCII, exactly at the cap, and it used to be refused.

    Nothing to do with ensure_ascii: json.dumps defaults to ", " and ": ", so a
    payload of exactly MAX_PAYLOAD_BYTES measured 65537. The audit describes
    only the non-ASCII half of this defect, so this case is recorded separately.
    """
    payload = {"a": "x" * (MAX_PAYLOAD_BYTES - 8)}
    assert payload_size_bytes(payload) == MAX_PAYLOAD_BYTES
    assert validate_app_message(_envelope(payload)) is not None

    over = {"a": "x" * (MAX_PAYLOAD_BYTES - 7)}
    assert payload_size_bytes(over) == MAX_PAYLOAD_BYTES + 1
    assert validate_app_message(_envelope(over)) is None


def test_lone_surrogate_is_measured_and_not_a_crash():
    """json.loads('{"a": "\\ud800"}') yields a lone surrogate.

    A plain ``.encode("utf-8")`` raises UnicodeEncodeError on it, and
    UnicodeEncodeError is a ValueError, so the validator's existing handler
    would have converted an acceptable payload into "not JSON-serialisable".
    JavaScript's well-formed JSON.stringify re-escapes it to six ASCII
    characters; the Python side must reach the same number.
    """
    payload = json.loads('{"a": "\\ud800"}')
    assert payload_size_bytes(payload) == 14
    assert validate_app_message(_envelope(payload)) is not None


def test_non_serialisable_payload_is_still_rejected():
    """Preservation guard: `default=str` handles what it can, the rest is refused."""
    circular: dict[str, Any] = {}
    circular["self"] = circular
    with pytest.raises(ValueError):
        payload_size_bytes(circular)
    assert validate_app_message(_envelope(circular)) is None


def test_typescript_mirror_asserts_the_same_fixture():
    """The done-when says the fixture is asserted in *both* suites.

    A Python-only assertion would leave the browser guard free to drift again,
    which is exactly how the two sides ended up 6x apart.
    """
    assert TS_MIRROR.exists(), f"missing TypeScript mirror test at {TS_MIRROR}"
    text = TS_MIRROR.read_text(encoding="utf-8")
    assert "app_message_payload_sizes.json" in text, (
        "the TS test must read the shared fixture, not a copy of the numbers"
    )
