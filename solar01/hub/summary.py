"""Headline numbers for the top of the console: today's energy, SOC against a day ago, the billing cycle.

Energy comes from the hourly table (the hub integrates the inverter's power readings every 2 s),
so today's figures and the cycle totals add up the same way.  "Grid" is import from the grid,
the part that is billed; "saved" is what the home used minus what came from the grid, over
the hours for which grid energy is known.
"""
from __future__ import annotations

import datetime as dt

from . import tariff as tariff_mod


def _kwh(v) -> float:
    return round((v or 0.0) / 1000, 2)


def cycle_summary(history, cal, on_peak: str, t: dict, now: dt.datetime) -> dict:
    start, end = tariff_mod.cycle_bounds(now.date(), t['cycle_start_day'])
    t0 = dt.datetime.combine(start, dt.time(0), cal.tz)
    t1 = dt.datetime.combine(end, dt.time(0), cal.tz)
    rows = history.hourly_energy(t0.timestamp(), now.timestamp() + 1)
    known = [r for r in rows if r[3] is not None]
    periods = tariff_mod.Periods(cal, on_peak)
    grid = tariff_mod.price([(r[0], r[3] / 1000) for r in known], t, periods)
    # what the same hours would have cost with everything the house used bought from the grid
    home = tariff_mod.price([(r[0], (r[1] or 0) / 1000) for r in known], t, periods)
    days = (end - start).days
    out = {
        'start': start.isoformat(), 'end': end.isoformat(), 'days': days,
        'day': (now.date() - start).days + 1, 'ends_ts': round(t1.timestamp()),
        'load_kwh': _kwh(sum(r[1] or 0 for r in rows)),
        'grid_kwh': round(grid['kwh'], 2), 'export_kwh': _kwh(sum(r[4] or 0 for r in known)),
        'known_load_kwh': round(home['kwh'], 2), 'saved_kwh': round(home['kwh'] - grid['kwh'], 2),
        'hours': len(rows), 'grid_hours': len(known),
        'grid_since': round(known[0][0]) if known else None,
        'partial': bool(rows) and (not known or known[0][0] > rows[0][0]),
        'configured': tariff_mod.configured(t),
    }
    if out['configured']:
        out.update(
            cost=round(grid['cost'], 2), home_cost=round(home['cost'], 2), saved=round(home['cost'] - grid['cost'], 2),
            by_period={k: {'kwh': round(v['kwh'], 2), 'cost': round(v['cost'], 2)} for k, v in grid['by_period'].items()},
            by_tier=[round(v, 2) for v in grid['by_tier']], tier=grid['tier'],
            avg_rate=round(grid['cost'] / grid['kwh'], 3) if grid['kwh'] > 0 else None,
            # straight line from the days with grid data; noisy in the first days of a cycle, so not given before one
            projected_cost=round(grid['cost'] / (len(known) / 24) * days, 2) if len(known) >= 24 else None)
    return out


def headline(history, cal, on_peak: str, t: dict, plan: dict, soc_now: float | None, now: dt.datetime) -> dict:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = history.hourly_energy(midnight.timestamp(), now.timestamp() + 1)
    grid_known = [r for r in today if r[3] is not None]
    prior = history.soc_near(now.timestamp() - 86400)
    return {
        'ts': round(now.timestamp()),
        'today': {
            'date': now.date().isoformat(),
            'pv_kwh': _kwh(sum(r[2] or 0 for r in today)),
            'load_kwh': _kwh(sum(r[1] or 0 for r in today)),
            'grid_kwh': _kwh(sum(r[3] for r in grid_known)) if grid_known else None,
            'export_kwh': _kwh(sum(r[4] or 0 for r in grid_known)) if grid_known else None,
            'pv_forecast_kwh': plan.get('pv_forecast_today_kwh'),
            'pv_forecast_tomorrow_kwh': plan.get('pv_forecast_tomorrow_kwh'),
        },
        'soc': {
            'now': None if soc_now is None else round(soc_now, 1),
            'prior': None if prior is None else round(prior[1], 1),
            'prior_ts': None if prior is None else prior[0],
            'delta': None if prior is None or soc_now is None else round(soc_now - prior[1], 1),
        },
        'cycle': cycle_summary(history, cal, on_peak, t, now),
    }
