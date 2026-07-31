"""ICS datetime parsing must handle the UTC shape, or the fallback is dead.

``CalendarIntegration._ics_dt`` used to do ``raw.replace("Z", "+00:00")`` then
slice ``raw[:len(fmt) + 4]``. Format-code width does not track data width
(``%Y`` is 2 characters of format for 4 of data, ``%z`` is 2 for 6), so the
UTC shape was cut from 21 characters to 19 (``20990101T090000+00:``) and every
format then failed. ``_ics_dt`` returned ``None``, ``_fetch_ics_events``
filtered the event out, and because effectively every real feed publishes UTC
stamps, the ICS calendar fallback reported an empty calendar for a feed full
of events.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from integrations.calendar import CalendarIntegration


class TestIcsDatetime:
    def test_utc_z_suffix_parses(self):
        """The shape that was completely broken, and the one real feeds use."""
        got = CalendarIntegration._ics_dt("20990101T090000Z")
        assert got is not None, "UTC stamp must parse, or every event is dropped"
        assert got == datetime(2099, 1, 1, 9, 0, tzinfo=timezone.utc)

    def test_explicit_offset_parses(self):
        got = CalendarIntegration._ics_dt("20990101T090000+00:00")
        assert got == datetime(2099, 1, 1, 9, 0, tzinfo=timezone.utc)

    def test_floating_local_parses(self):
        got = CalendarIntegration._ics_dt("20990101T090000")
        assert got == datetime(2099, 1, 1, 9, 0)
        assert got.tzinfo is None

    def test_date_only_parses(self):
        assert CalendarIntegration._ics_dt("20990101") == datetime(2099, 1, 1, 0, 0)

    def test_crlf_trailing_whitespace_tolerated(self):
        """ICS is a CRLF format; a stray \\r must not defeat parsing."""
        assert CalendarIntegration._ics_dt("20990101T090000Z\r\n") is not None

    @pytest.mark.parametrize("raw", ["", "   ", "garbage", "not-a-date"])
    def test_unparseable_returns_none(self, raw):
        assert CalendarIntegration._ics_dt(raw) is None

    def test_none_input_is_survivable(self):
        assert CalendarIntegration._ics_dt(None) is None

    def test_a_utc_feed_yields_events_not_an_empty_calendar(self):
        """The user-visible contract: a feed of UTC events is not 'empty'."""
        stamps = ["20990101T090000Z", "20990102T140000Z", "20990103T173000Z"]
        parsed = [CalendarIntegration._ics_dt(s) for s in stamps]
        assert all(p is not None for p in parsed)
        assert len([p for p in parsed if p]) == 3
