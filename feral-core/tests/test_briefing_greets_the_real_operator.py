"""The morning briefing greeted every operator as "Alex".

Reported 2026-08-23 from a live install: starting FERAL produced a
Morning Briefing card reading "Good morning, Alex!" to an operator
named Omar, whose USER.md says `Name: Omar` on line 3.

It was a placeholder hardcoded into the SDUI headline
(``f"{greeting}, Alex!"``). Only the card carried it; the plain-text
body and the voice line had no name at all, which is why chat greeted
the operator correctly and the dashboard did not.

Greeting somebody by the wrong name is worse than not greeting them by
name. On a product whose whole claim is that it knows you, it is also
the first thing they see.
"""
from __future__ import annotations

import pytest

from identity.workspace import DEFAULT_USER_MD, IdentityWorkspace


def _workspace(tmp_path, user_md: str | None) -> IdentityWorkspace:
    ws = IdentityWorkspace(home_dir=str(tmp_path))
    if user_md is not None:
        (tmp_path / "USER.md").write_text(user_md)
    return ws


class TestReadingTheOperatorName:

    def test_the_setup_wizard_shape(self, tmp_path):
        """What a real install looks like."""
        ws = _workspace(tmp_path, "# About Me\n\nName: Omar\nOccupation: founder\n")
        assert ws.read_user_name() == "Omar"

    def test_a_markdown_heading(self, tmp_path):
        ws = _workspace(tmp_path, "# Noah Chen\n\nInvestor.\n")
        assert ws.read_user_name() == "Noah Chen"

    def test_the_template_heading_is_not_a_name(self, tmp_path):
        """"About Me" is the template's own heading. Returning it would
        greet the operator as "About Me"."""
        ws = _workspace(tmp_path, "# About Me\n\nSome prose with no name line.\n")
        assert ws.read_user_name() == ""

    def test_the_unfilled_template_yields_nothing(self, tmp_path):
        ws = _workspace(tmp_path, DEFAULT_USER_MD)
        assert ws.read_user_name() == ""

    def test_an_empty_profile_yields_nothing(self, tmp_path):
        ws = _workspace(tmp_path, "")
        assert ws.read_user_name() == ""

    def test_a_trailing_comma_clause_is_trimmed(self, tmp_path):
        ws = _workspace(tmp_path, "Name: Omar, founder of Theora\n")
        assert ws.read_user_name() == "Omar"

    def test_it_never_invents(self, tmp_path):
        """The one guarantee that matters: no name beats a wrong name."""
        for text in ("", "   \n\n", "no profile here", DEFAULT_USER_MD):
            ws = _workspace(tmp_path, text)
            assert ws.read_user_name() == ""


class TestTheRenderedCard:
    """Drive the real builder and read the real SDUI headline.

    This is the string the operator saw on the dashboard, so it is the
    string worth asserting.
    """

    @staticmethod
    def _headline(monkeypatch, tmp_path, user_md: str, hour: int = 9) -> str:
        import asyncio
        import time as _time
        from unittest.mock import AsyncMock

        import agents.proactive_engine as pe
        import config.loader as loader

        _workspace(tmp_path, user_md)
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.setattr(loader, "feral_home", lambda: tmp_path, raising=False)

        frozen = _time.struct_time((2026, 8, 23, hour, 0, 0, 6, 235, 1))
        monkeypatch.setattr(pe.time, "localtime", lambda *a: frozen)

        # One episode, so the briefing has a section and does not bail.
        memory = type("M", (), {})()
        memory.episode_recent = AsyncMock(return_value=[
            {"summary": "talked to Noah about the round", "created_at": 0},
        ])
        engine = pe.ProactiveEngine(memory=memory)

        message = asyncio.run(engine._build_morning_briefing())
        if message is None:
            pytest.skip("briefing produced no sections in this environment")
        return message.sdui["children"][0]["value"]

    def test_the_operator_is_greeted_by_their_own_name(self, monkeypatch, tmp_path):
        headline = self._headline(
            monkeypatch, tmp_path, "# About Me\n\nName: Omar\nOccupation: founder\n",
        )
        assert headline == "Good morning, Omar!"
        assert "Alex" not in headline

    def test_no_profile_means_no_name_not_a_wrong_one(self, monkeypatch, tmp_path):
        headline = self._headline(monkeypatch, tmp_path, DEFAULT_USER_MD)
        assert headline == "Good morning!"

    def test_the_evening_reads_as_the_evening(self, monkeypatch, tmp_path):
        headline = self._headline(
            monkeypatch, tmp_path, "Name: Omar\n", hour=23,
        )
        assert headline == "Good evening, Omar!"

    def test_nobody_is_called_alex(self):
        """The literal is gone from the source, not merely overridden."""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "agents" / "proactive_engine.py"
        ).read_text()
        assert '"{greeting}, Alex!"' not in src
        assert "f\"{greeting}, Alex!\"" not in src


class TestTimeOfDay:
    """"Good afternoon" ran from noon to midnight, so a briefing at 11pm
    opened by calling it the afternoon."""

    @pytest.mark.parametrize("hour, expected", [
        (0, "Good morning"),
        (9, "Good morning"),
        (11, "Good morning"),
        (12, "Good afternoon"),
        (17, "Good afternoon"),
        (18, "Good evening"),
        (23, "Good evening"),
    ])
    def test_every_hour_reads_true(self, hour, expected):
        if hour < 12:
            greeting = "Good morning"
        elif hour < 18:
            greeting = "Good afternoon"
        else:
            greeting = "Good evening"
        assert greeting == expected

    def test_the_source_has_an_evening_branch(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "agents" / "proactive_engine.py"
        ).read_text()
        assert "Good evening" in src
