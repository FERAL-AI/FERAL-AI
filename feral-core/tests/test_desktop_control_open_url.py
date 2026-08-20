"""``open_url`` exists because the obvious path was a documented dead end.

Asked to "open a YouTube song on Chrome", the router picks
``desktop_control``, the model writes the natural AppleScript for a web
page, and ``SandboxPolicy.validate_applescript`` refuses it: ``open
location`` is on the denied-phrase list because it dispatches *any* URL
scheme, including ``file://`` and an app's private scheme.

The refusal was correct. What was broken is that ``open_app``'s own
description listed the phrases the policy rejects and left ``open
location`` out of the list, so the model was handed an incomplete
contract, wrote something not on it, and got a 403. There was a
permitted path the whole time (the allowlisted ``open`` program) and
nothing pointed at it.

These tests hold the three things that keep that from recurring: the
permitted path works, the refusals are real, and the description does
not drift from the policy again.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from security.sandbox_policy import SandboxPolicy
from skills.impl.desktop_control import (
    ALLOWED_URL_SCHEMES,
    DesktopControlSkill,
    _validate_app,
    _validate_url,
)

MANIFEST = Path(__file__).resolve().parents[1] / "skills" / "manifests" / "desktop_control.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _endpoint(manifest: dict, endpoint_id: str) -> dict:
    for ep in manifest["endpoints"]:
        if ep["id"] == endpoint_id:
            return ep
    raise AssertionError(f"{endpoint_id} missing from desktop_control.json")


class TestTheDocumentedContractMatchesThePolicy:
    """The bug was a description that disagreed with the validator."""

    # Every phrase the AppleScript validator refuses. Written out rather
    # than scraped from the policy source, so adding a phrase to the
    # policy without documenting it fails here instead of passing
    # vacuously.
    DENIED_PHRASES = [
        "do shell script",
        "do script",
        "run script",
        "load script",
        "osascript",
        "use framework",
        "current application's",
        "NSTask",
        "system attribute",
        "open location",
    ]

    @pytest.mark.parametrize("phrase", DENIED_PHRASES)
    def test_each_documented_phrase_is_really_refused(self, phrase):
        ok, _ = SandboxPolicy.load_default().validate_applescript(
            f'tell application "X" to {phrase}'
        )
        assert not ok, f"{phrase!r} is documented as refused but the policy allows it"

    @pytest.mark.parametrize("phrase", DENIED_PHRASES)
    def test_each_refused_phrase_is_named_in_the_description(self, phrase, manifest):
        """This is the assertion that would have caught the original bug.

        ``open location`` was refused by the policy and absent from the
        description, which is exactly how a model ends up writing a
        script that cannot run.
        """
        desc = _endpoint(manifest, "open_app")["description"]
        assert phrase in desc, (
            f"the policy refuses {phrase!r} and open_app's description does not "
            "say so. A model reading that description will write it and get a 403."
        )

    def test_the_form_the_description_recommends_actually_passes(self, manifest):
        """A contract that names only refusals still has to leave a way through."""
        ok, reason = SandboxPolicy.load_default().validate_applescript(
            'tell application "Google Chrome" to activate'
        )
        assert ok, f"the recommended form is refused: {reason}"


class TestTheURLPathIsNarrow:
    """``open location`` was refused for dispatching arbitrary schemes.
    A replacement that reintroduced that would be a regression, not a fix.
    """

    def test_only_http_and_https_are_allowed(self):
        assert ALLOWED_URL_SCHEMES == frozenset({"http", "https"})

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/results?search_query=lofi%20hip%20hop",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s",
        "http://example.com",
    ])
    def test_a_real_web_url_passes(self, url):
        parsed, reason = _validate_url(url)
        assert parsed == url, reason

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>1</script>",
        "x-apple-shortcuts://run-shortcut?name=anything",
        "ftp://example.com",
    ])
    def test_every_other_scheme_is_refused(self, url):
        parsed, reason = _validate_url(url)
        assert parsed is None, f"{url!r} was accepted; open_url must stay http/https only"
        assert reason

    @pytest.mark.parametrize("url", ["", None, 123, "   ", "no-scheme-at-all"])
    def test_junk_is_refused_rather_than_shelled(self, url):
        parsed, _ = _validate_url(url)
        assert parsed is None

    def test_a_url_cannot_smuggle_an_open_flag(self):
        """``open -a Calculator`` as a 'URL' must not become argv flags."""
        parsed, _ = _validate_url("-a /Applications/Calculator.app")
        assert parsed is None


class TestTheAppNameIsAnAppName:
    @pytest.mark.parametrize("app", ["Google Chrome", "Safari", "Brave Browser", "Firefox"])
    def test_real_browsers_pass(self, app):
        _, reason = _validate_app(app)
        assert not reason, reason

    @pytest.mark.parametrize("app", ["; rm -rf /", "../../bin/sh", "-a", "$(whoami)", "a|b"])
    def test_anything_shell_shaped_is_refused(self, app):
        _, reason = _validate_app(app)
        assert reason, f"{app!r} was accepted as an application name"

    def test_the_app_is_optional(self):
        """Omitting it uses the default browser, which is usually right."""
        _, reason = _validate_app(None)
        assert not reason


class TestTheEndpointIsDiscoverable:
    """A capability the router cannot find is a capability that does not exist.
    The original failure was a routing failure as much as a policy one.
    """

    def test_open_url_is_declared(self, manifest):
        ep = _endpoint(manifest, "open_url")
        assert ep["method"] == "PYTHON"
        assert {p["name"] for p in ep["params"]} == {"url", "app"}

    def test_the_description_points_away_from_the_dead_end(self, manifest):
        """Naming the trap is what stops the model walking back into it."""
        desc = _endpoint(manifest, "open_url")["description"]
        assert "open location" in desc
        assert "open_app" in desc

    @pytest.mark.parametrize("phrase", [
        "open a website", "open a url", "open youtube",
        "play something on youtube", "open this in chrome",
    ])
    def test_web_page_phrasings_are_triggers(self, phrase, manifest):
        assert phrase in manifest["trigger_phrases"], (
            f"{phrase!r} is not a trigger, so this skill may not be offered "
            "for the request that motivated open_url"
        )


class TestTheSkillClaimsWhatTheManifestDeclares:
    def test_every_manifest_endpoint_is_handled(self, manifest):
        """A Python backing implementation claims *every* endpoint of its
        skill, so a declared-but-unrouted endpoint is a 404 at runtime
        rather than a fall-through to the old daemon lane.

        Probed through ``execute`` with empty args: a handled endpoint
        refuses on its missing parameter, an unhandled one 404s. Empty
        args mean nothing is executed, so this stays side-effect free.
        """
        import asyncio

        skill = DesktopControlSkill()
        unrouted = []
        for ep in manifest["endpoints"]:
            result = asyncio.run(skill.execute(ep["id"], {}, {}))
            if result.get("status_code") == 404:
                unrouted.append(ep["id"])
        assert unrouted == [], (
            f"declared in desktop_control.json with no dispatch entry: {unrouted}"
        )

    def test_an_undeclared_endpoint_still_404s(self):
        """Keeps the check above honest: 404 has to mean something."""
        import asyncio

        skill = DesktopControlSkill()
        result = asyncio.run(skill.execute("no_such_endpoint", {}, {}))
        assert result["status_code"] == 404
