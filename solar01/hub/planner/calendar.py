"""SDG&E TOU calendar: super-off-peak windows, weekends and observed holidays.

The user's plan: SOP 00-06 and 10-14 on weekdays, 00-14 on weekends and holidays.
(Real TOU-DR1 has the weekday 10-14 window only in March and April; the windows are
configurable.)
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo


def _nth_weekday(year, month, weekday, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


@lru_cache(maxsize=16)
def holidays(year: int) -> frozenset:
    """New Year, Presidents', Memorial, Independence, Labor, Veterans, Thanksgiving, Christmas."""
    fixed = [dt.date(year, 1, 1), dt.date(year, 7, 4), dt.date(year, 11, 11), dt.date(year, 12, 25)]
    floating = [_nth_weekday(year, 2, 0, 3), _last_weekday(year, 5, 0), _nth_weekday(year, 9, 0, 1),
                _nth_weekday(year, 11, 3, 4)]
    return frozenset({_observed(d) for d in fixed} | set(floating))


def parse_windows(s: str) -> list[tuple[int, int]]:
    out = []
    for part in s.split(','):
        part = part.strip()
        if part:
            a, b = part.split('-')
            out.append((int(a), int(b)))
    return out


class TouCalendar:
    def __init__(self, tz, weekday_sop: str = '0-6,10-14', weekend_sop: str = '0-14', extra_holidays=()):
        self.tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        self.weekday = parse_windows(weekday_sop)
        self.weekend = parse_windows(weekend_sop)
        self.extra = {str(s).strip() for s in extra_holidays if str(s).strip()}

    @classmethod
    def from_config(cls, site, planner) -> 'TouCalendar':
        return cls(site.tz, planner.weekday_sop, planner.weekend_sop, planner.extra_holidays)

    def is_offpeak_day(self, d: dt.date) -> bool:
        return d.weekday() >= 5 or d in holidays(d.year) or d.isoformat() in self.extra

    def sop_hours(self, d: dt.date):
        return self.weekend if self.is_offpeak_day(d) else self.weekday

    def sop_windows(self, t: dt.datetime, days: int = 3):
        """Aware (start, end) datetimes of all SOP windows from t's date over `days` days."""
        out = []
        d0 = t.date()
        for i in range(days):
            d = d0 + dt.timedelta(days=i)
            for a, b in self.sop_hours(d):
                ws = dt.datetime.combine(d, dt.time(a), self.tz)
                we = dt.datetime.combine(d, dt.time(0), self.tz) + dt.timedelta(hours=b)
                out.append((ws, we))
        out.sort()
        return out

    def current_window(self, t: dt.datetime):
        for ws, we in self.sop_windows(t):
            if ws <= t < we:
                return ws, we
        return None

    def next_window_start(self, t: dt.datetime):
        for ws, _we in self.sop_windows(t, 8):
            if ws >= t:
                return ws
        return None
