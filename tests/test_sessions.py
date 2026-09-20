"""
The 22:00-06:00 New York window crosses midnight, sits in a DST zone, and must
not open on Friday or Saturday nights when spot FX is shut.
"""
from datetime import datetime, timezone

import pytest

from sessions import SessionWindow, from_config


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


@pytest.fixture
def window():
    return SessionWindow()


# 2026-01-14 is a Wednesday. EST = UTC-5, so 22:00 local == 03:00 UTC Thursday.
def test_closed_one_minute_before_open(window):
    assert window.is_open(utc(2026, 1, 15, 2, 59)) is False


def test_open_exactly_at_2200(window):
    assert window.is_open(utc(2026, 1, 15, 3, 0)) is True


def test_open_after_midnight_same_session(window):
    """02:00 Thursday belongs to Wednesday's session, not Thursday's."""
    assert window.session_key(utc(2026, 1, 15, 7, 0)) == "2026-01-14"


def test_whole_window_shares_one_session_key(window):
    keys = {window.session_key(utc(2026, 1, 15, h)) for h in (3, 5, 7, 9, 10)}
    assert keys == {"2026-01-14"}


def test_closed_exactly_at_0600(window):
    assert window.is_open(utc(2026, 1, 15, 11, 0)) is False


def test_closed_during_the_day(window):
    assert window.is_open(utc(2026, 1, 15, 18, 0)) is False


def test_dst_shifts_the_utc_hour(window):
    """In July, New York is UTC-4, so the same local 22:00 is an hour earlier in UTC."""
    assert window.is_open(utc(2026, 7, 16, 2, 0)) is True          # 22:00 EDT
    assert window.is_open(utc(2026, 7, 16, 1, 59)) is False        # 21:59 EDT
    # The UTC hour that was 22:00 in winter is 23:00 in summer — still open,
    # but a fixed-offset implementation would have got the boundary wrong.
    assert window.session_key(utc(2026, 7, 16, 3, 0)) == "2026-07-15"


def test_friday_night_is_shut(window):
    """FX closes 17:00 Friday New York; a 22:00 Friday window has no market."""
    assert window.is_open(utc(2026, 1, 17, 3, 0)) is False         # Fri 22:00 EST


def test_saturday_night_is_shut(window):
    assert window.is_open(utc(2026, 1, 18, 3, 0)) is False         # Sat 22:00 EST


def test_sunday_night_opens_the_week(window):
    assert window.session_key(utc(2026, 1, 19, 3, 0)) == "2026-01-18"


def test_monday_small_hours_belong_to_sundays_session(window):
    assert window.session_key(utc(2026, 1, 19, 8, 0)) == "2026-01-18"


def test_naive_datetime_is_treated_as_utc(window):
    aware = window.session_key(utc(2026, 1, 15, 7, 0))
    naive = window.session_key(datetime(2026, 1, 15, 7, 0))
    assert aware == naive


def test_non_midnight_crossing_window():
    day = SessionWindow(start="08:00", end="16:00")
    assert day.is_open(utc(2026, 1, 14, 14, 0)) is True            # 09:00 EST Wed
    assert day.is_open(utc(2026, 1, 14, 2, 0)) is False            # 21:00 EST Tue


def test_from_config_reads_the_session_block():
    w = from_config({"session": {"timezone": "Europe/London",
                                 "start": "07:00", "end": "16:00"}})
    assert w.tz_name == "Europe/London"
    assert not w.crosses_midnight


def test_from_config_defaults_when_absent():
    w = from_config({})
    assert w.tz_name == "America/New_York"
    assert w.crosses_midnight


def test_bad_timezone_raises():
    with pytest.raises(Exception):
        SessionWindow(tz_name="Not/AZone")
