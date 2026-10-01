"""
sessions.py — the trading window, and the session identity used for per-session
counters.

The configured window (22:00-06:00 America/New_York by default) crosses
midnight and sits inside a timezone with daylight saving, so neither "hour >=
22" nor a fixed UTC offset is correct year-round. Everything here converts to
the configured zone with zoneinfo and lets it handle DST.

A "session" is one overnight window, identified by the calendar date of its
22:00 open in local terms. That identity is what "3 trades per session" counts
against — a counter keyed on the UTC date would reset at midnight, halfway
through the window.

Spot FX trades from Sunday 17:00 to Friday 17:00 New York. A window opening at
22:00 on Friday or Saturday therefore has no market behind it, and is excluded.
"""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/New_York"
DEFAULT_START = "22:00"
DEFAULT_END = "06:00"


def _parse_hhmm(text: str) -> time:
    hh, mm = str(text).split(":")
    return time(int(hh), int(mm))


class SessionWindow:
    def __init__(self, tz_name: str = DEFAULT_TZ,
                 start: str = DEFAULT_START, end: str = DEFAULT_END):
        self.tz = ZoneInfo(tz_name)
        self.tz_name = tz_name
        self.start = _parse_hhmm(start)
        self.end = _parse_hhmm(end)
        self.crosses_midnight = self.start > self.end

    def local(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(self.tz)

    def is_open(self, moment: datetime) -> bool:
        """Is `moment` (UTC or aware) inside the trading window?"""
        return self.session_key(moment) is not None

    def session_key(self, moment: datetime) -> str | None:
        """
        Identity of the session `moment` falls in, or None if outside it.

        The key is the local date on which the window OPENED, so the whole
        overnight run shares one key across the midnight boundary.
        """
        here = self.local(moment)
        t = here.time()

        if self.crosses_midnight:
            if t >= self.start:
                open_date = here.date()            # before midnight
            elif t < self.end:
                open_date = here.date() - timedelta(days=1)   # after midnight
            else:
                return None
        else:
            if not (self.start <= t < self.end):
                return None
            open_date = here.date()

        # Monday=0 ... Friday=4, Saturday=5, Sunday=6. A window opening Friday
        # or Saturday night has no open market behind it.
        if open_date.weekday() in (4, 5):
            return None
        return open_date.isoformat()

    def describe(self) -> str:
        return (f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')} "
                f"{self.tz_name}")


def from_config(cfg: dict) -> SessionWindow:
    s = (cfg.get("session") or {})
    return SessionWindow(
        tz_name=s.get("timezone", DEFAULT_TZ),
        start=s.get("start", DEFAULT_START),
        end=s.get("end", DEFAULT_END),
    )
