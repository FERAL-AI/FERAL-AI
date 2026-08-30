"""Published docs must not advertise settings the brain never reads.

``docs/mintlify/hardware/glasses.mdx`` shipped a "Supported Devices"
table listing Meta Ray-Ban and Vuzix Blade 2, a ``hardware.glasses``
settings block with ``ar_overlay`` / ``stream_resolution`` /
``stream_fps``, and troubleshooting steps telling operators to change
those values. Not one of those strings existed anywhere in the source.

That is worse than a stale doc. It is a public page that could persuade
someone to buy a Vuzix Blade 2, and a support instruction ("verify
``ar_overlay`` is true in settings") that can never succeed because the
key is read by nothing.

Docs legitimately describe roadmap and concepts, so a blanket "every
identifier in the docs must exist" rule would be noise. This guard is
narrow: a small list of setting keys that were found to be fabricated,
asserted absent from the docs *as configuration* unless they appear
inside an explicit correction. If a feature is genuinely implemented
later, the key appears in the source and this test stops applying to it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keys the docs presented as configuration that no source file reads.
FABRICATED_SETTING_KEYS = (
    "ar_overlay",
    "stream_resolution",
    "stream_fps",
)

_SOURCE_DIRS = ("api", "agents", "hardware", "memory", "perception", "skills", "config")


def _source_mentions(key: str) -> list[str]:
    hits = []
    for d in _SOURCE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in list(base.rglob("*.py")) + list(base.rglob("*.json")):
            if "build/" in str(p.relative_to(ROOT)):
                continue
            try:
                if key in p.read_text():
                    hits.append(str(p.relative_to(ROOT)))
            except (UnicodeDecodeError, OSError):
                continue
    return hits


@pytest.mark.parametrize("key", FABRICATED_SETTING_KEYS)
def test_the_key_is_still_absent_from_the_source(key):
    """If someone implements it, this test should be revisited, not kept.

    A failure here is good news: it means the feature became real. Remove
    the key from FABRICATED_SETTING_KEYS when that happens.
    """
    hits = _source_mentions(key)
    assert not hits, (
        f"{key!r} now exists in the source ({hits}); it is no longer "
        "fabricated. Drop it from FABRICATED_SETTING_KEYS and let the docs "
        "describe it."
    )


def _glasses_doc() -> str:
    return (REPO / "docs" / "mintlify" / "hardware" / "glasses.mdx").read_text()


@pytest.mark.parametrize("key", FABRICATED_SETTING_KEYS)
def test_the_docs_do_not_present_the_key_as_configuration(key):
    """Mentioning it inside a correction is fine. Instructing it is not.

    The heuristic: a line that both names the key and reads like a
    setting the operator should change. Correction paragraphs name the
    key while explaining it does not exist, and those must stay.
    """
    instructing = re.compile(
        r"^(?!.*(?:previously|no longer|never|not exist|none of that|there is no|existed))"
        r".*\b" + re.escape(key) + r"\b.*"
        r"(?:\bset\b|\bverify\b|\bdefault\b|\breduce\b|\benable\b|:\s*(?:true|\d))",
        re.IGNORECASE,
    )
    offenders = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(_glasses_doc().splitlines(), 1)
        if instructing.match(line)
    ]
    assert not offenders, (
        f"the glasses doc still instructs operators to configure {key!r}, "
        "which the brain reads nowhere:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("vendor", ["Vuzix", "Ray-Ban"])
def test_the_docs_do_not_claim_vendor_support_that_does_not_exist(vendor):
    """A supported-devices table is a purchasing recommendation."""
    doc = _glasses_doc()
    # Allowed only inside the correction that names them as removed.
    for line in doc.splitlines():
        if vendor.lower() in line.lower():
            assert re.search(
                r"previously|earlier versions|none of that|no longer",
                line,
                re.IGNORECASE,
            ) or "listed" in line.lower(), (
                f"the glasses doc mentions {vendor} outside a correction: "
                f"{line.strip()!r}. No driver for it exists."
            )


def test_the_correction_itself_is_present():
    """Guards against 'fixing' this by deleting the page's history.

    Someone landing on this page after buying hardware on the strength of
    the old table deserves to see that it was wrong, not a silently
    rewritten page.
    """
    doc = _glasses_doc()
    assert "None of that existed" in doc
