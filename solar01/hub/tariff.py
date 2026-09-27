"""Electricity tariff: TOU rates per period, usage-tier adders per billing cycle, and the billing cycle itself.

Pure functions.  The settings are edited in the web console and kept in the history database
(kv 'tariff'), not in config.toml, because the hub cannot write /etc/solar01.

    {"cycle_start_day": 5,                                   1..31, clamped to the month's length
     "rates": {"super_off_peak": 0.12, "off_peak": 0.45, "on_peak": 0.62},       $/kWh
     "tiers": [{"up_to_kwh": 300, "adder": -0.1}, {"up_to_kwh": null, "adder": 0}]}

Each kWh costs the rate of the TOU period it was used in plus the adder of the usage tier the
cycle's running total is in (a kWh that crosses a tier boundary is split).  No tiers means no
adders.  The last tier is open-ended (up_to_kwh null).
"""
from __future__ import annotations

import calendar as pycal
import datetime as dt

from .planner.calendar import parse_windows

PERIODS = ('super_off_peak', 'off_peak', 'on_peak')
MAX_TIERS = 6

DEFAULT = {'cycle_start_day': 1, 'rates': {k: 0.0 for k in PERIODS}, 'tiers': []}


def _number(v, name, lo=None, hi=None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f'{name} must be a number')
    v = float(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ValueError(f'{name} must be between {lo} and {hi}')
    return v


def validate(data) -> dict:
    """A clean copy of the settings, or ValueError naming the first bad field."""
    if not isinstance(data, dict):
        raise ValueError('expected an object')
    day = data.get('cycle_start_day', 1)
    if isinstance(day, bool) or not isinstance(day, (int, float)) or int(day) != day or not 1 <= day <= 31:
        raise ValueError('cycle_start_day must be a whole number from 1 to 31')
    rates_in = data.get('rates') or {}
    if not isinstance(rates_in, dict):
        raise ValueError('rates must be an object')
    rates = {k: _number(rates_in.get(k, 0.0), f'rates.{k}', 0, 10) for k in PERIODS}
    tiers_in = data.get('tiers') or []
    if not isinstance(tiers_in, list) or len(tiers_in) > MAX_TIERS:
        raise ValueError(f'tiers must be a list of at most {MAX_TIERS}')
    tiers, prev = [], 0.0
    for i, t in enumerate(tiers_in):
        if not isinstance(t, dict):
            raise ValueError(f'tier {i + 1} must be an object')
        last = i == len(tiers_in) - 1
        up = t.get('up_to_kwh')
        if last:
            if up is not None:
                raise ValueError('the last tier has no upper limit (up_to_kwh null)')
        else:
            up = _number(up, f'tier {i + 1} up_to_kwh', 0, 1e6)
            if up <= prev:
                raise ValueError(f'tier {i + 1} limit must be above {prev:g} kWh')
            prev = up
        tiers.append({'up_to_kwh': up, 'adder': _number(t.get('adder', 0.0), f'tier {i + 1} adder', -10, 10)})
    return {'cycle_start_day': int(day), 'rates': rates, 'tiers': tiers}


def configured(t: dict) -> bool:
    return any(t['rates'][k] for k in PERIODS)


def _start_in(year: int, month: int, day: int) -> dt.date:
    return dt.date(year, month, min(day, pycal.monthrange(year, month)[1]))


def cycle_bounds(today: dt.date, start_day: int) -> tuple[dt.date, dt.date]:
    """(first day, first day of the next cycle) of the billing cycle containing today."""
    start = _start_in(today.year, today.month, start_day)
    if today < start:
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        start = _start_in(y, m, start_day)
    y, m = (start.year, start.month + 1) if start.month < 12 else (start.year + 1, 1)
    return start, _start_in(y, m, start_day)


class Periods:
    """TOU period of an hour: super off-peak from the planner calendar, on-peak from the config, else off-peak."""

    def __init__(self, cal, on_peak: str):
        self.cal = cal
        self.on_peak = parse_windows(on_peak)

    def at(self, t: dt.datetime) -> str:
        h = t.hour
        if any(a <= h < b for a, b in self.cal.sop_hours(t.date())):
            return 'super_off_peak'
        if any(a <= h < b for a, b in self.on_peak):
            return 'on_peak'
        return 'off_peak'


def price(hours, t: dict, periods: Periods) -> dict:
    """Cost of [(hour ts, kWh)] in time order, starting at the beginning of a billing cycle.
    Returns total cost and kWh, kWh and cost per TOU period, kWh per tier and the tier reached."""
    by_period = {k: {'kwh': 0.0, 'cost': 0.0} for k in PERIODS}
    tiers = t['tiers']
    by_tier = [0.0] * len(tiers)
    used = cost = 0.0
    for ts, kwh in hours:
        if not kwh or kwh <= 0:
            continue
        period = periods.at(dt.datetime.fromtimestamp(ts, periods.cal.tz))
        c = kwh * t['rates'][period]
        left = kwh
        for i, tier in enumerate(tiers):
            if left <= 0:
                break
            room = left if tier['up_to_kwh'] is None else max(0.0, tier['up_to_kwh'] - (used + kwh - left))
            part = min(left, room)
            if part > 0:
                c += part * tier['adder']
                by_tier[i] += part
                left -= part
        used += kwh
        cost += c
        by_period[period]['kwh'] += kwh
        by_period[period]['cost'] += c
    tier_now = None
    if tiers:
        tier_now = next((i for i, tier in enumerate(tiers) if tier['up_to_kwh'] is None or used < tier['up_to_kwh']),
                        len(tiers) - 1)
    return {'kwh': used, 'cost': cost, 'by_period': by_period, 'by_tier': by_tier, 'tier': tier_now}
