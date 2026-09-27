"""SOC simulation and the hold / grid-charge decision.  Pure functions.

Behaviour is ported unchanged from solar_tou.py as it ran live 2026-09-09..13, with
one addition: the battery charge cap (``chg_cap_kw``) is a parameter so the planner
can pass the inverter's real limit instead of a constant, and the charge duration is
computed with the capped rate.

Decision rules, in order, inside a super-off-peak (SOP) window:

1. Simulate to the start of the next SOP window with no hold and no grid charge.
   If SOC stays above the reserve: do nothing.
2. Otherwise find the shortest standby hold that closes the shortfall, ending as late
   as possible: at the stop time or when PV is expected, whichever is first.  Holding is
   skipped if it would make PV clip later.
3. If no hold is enough: the longest possible hold, then the smallest quick-charge
   target started as late as possible; standby ends window_exit_lead_min before it.
   Already below the reserve: the charge starts now, and the smallest target that keeps
   SOC above the reserve from the stop time to the horizon is chosen.

Every hold and charge ends window_exit_lead_min before the window closes (the stop time),
because the inverter needs minutes to return to normal; in those last minutes nothing
starts.  Late scheduling lets every later tick re-decide on fresher data.

rolling_projection() extends the decision's projection to a full day by previewing what
the planner would decide at the start of each later window.
"""
from __future__ import annotations

import datetime as dt
import math


def simulate(p, t0, soc0, horizon, batt_kwh, load_fn, pv_fn, charge_to=None, charge_kw=0.0, we=None,
             hold_until=None, charge_from=None, soc_log=None, hold_from=None, chg_cap_kw=None):
    """Step SOC forward in p.sim_step_s steps.

    hold_from/hold_until: while inside and PV < load, the inverter is in standby (grid feeds
    the loads, battery untouched).  charge_from: grid charging (to charge_to, inside the
    window ending at we) starts no earlier than this.  soc_log receives (step end, soc).
    Returns (min_soc, trace[(label, load_kw, pv_kw, soc)], clipped_kwh, final_soc)."""
    cap = p.pv_chg_max_kw if chg_cap_kw is None else chg_cap_kw
    e = soc0 / 100 * batt_kwh
    min_soc = soc0
    trace = []
    clipped = 0.0
    t = t0
    step = dt.timedelta(seconds=p.sim_step_s)
    dh = p.sim_step_s / 3600
    while t < horizon:
        load = load_fn(t) * p.load_margin / 1000
        pv = pv_fn(t) * p.pv_margin / 1000
        net = pv - load
        soc = e / batt_kwh * 100
        frac = 0.0                                                          # share of this step spent grid charging
        if charge_to is not None and we is not None and soc < charge_to:
            c0 = t if charge_from is None else max(t, charge_from)
            c1 = min(t + step, we)
            frac = max(0.0, (c1 - c0).total_seconds()) / p.sim_step_s
        if frac > 0:
            e += min(charge_kw + max(net, 0.0), cap) * p.chg_eff * dh * frac   # loads on grid
            e = min(e, charge_to / 100 * batt_kwh)                              # inverter stops at the limit
        rest = dh * (1.0 - frac)
        if rest > 0:
            if net >= 0:
                e += min(net, cap) * p.chg_eff * rest
            elif hold_until is not None and t < hold_until and (hold_from is None or t >= hold_from):
                pass                                                            # standby: grid feeds loads
            else:
                e -= min(-net, p.dis_max_kw) / p.dis_eff * rest
        if e > batt_kwh:
            clipped += e - batt_kwh
            e = batt_kwh
        soc = e / batt_kwh * 100
        min_soc = min(min_soc, soc)
        e = max(e, 0.0)
        if (t - t0).total_seconds() % 3600 < p.sim_step_s:
            trace.append((t.strftime('%a %H:%M'), round(load, 2), round(pv, 2), round(soc)))
        if soc_log is not None:
            soc_log.append((t + step, soc))
        t += step
    return min_soc, trace, clipped, e / batt_kwh * 100


def _projection(now, soc, log):
    return [[round(now.timestamp()), round(soc, 1)]] + [[round(t.timestamp()), round(v, 1)] for t, v in log]


def _min_at(now, soc, log, min_soc):
    if not log or soc <= min_soc + 1e-6:
        return now.isoformat(timespec='minutes')
    return min(log, key=lambda r: r[1])[0].isoformat(timespec='minutes')


def decide(p, cal, now, soc, batt_kwh, load_fn, pv_fn, charge_kw, currently_on, hold_enabled=None,
           pv_now_w=None, currently_holding=False, chg_cap_kw=None):
    """Plan dict for this tick.  pv_now_w: measured PV (None while in standby).
    currently_holding: the inverter is in standby now (hysteresis; an ongoing hold is kept)."""
    if hold_enabled is None:
        hold_enabled = p.hold_enabled
    cap = p.pv_chg_max_kw if chg_cap_kw is None else chg_cap_kw
    plan = {'soc': soc, 'reserve_soc': p.reserve_soc, 'batt_kwh': round(batt_kwh, 2), 'charge_kw': round(charge_kw, 2),
            'chg_cap_kw': round(cap, 2), 'hold': False}
    win = cal.current_window(now)
    if win is None:
        nw = cal.next_window_start(now)
        plan.update(action='off', in_sop=False, reason='not super off-peak', next_window=nw.isoformat(timespec='minutes'),
                    target_soc=None, grid_kwh=0.0)
        log = []
        min_soc, trace, _, final = simulate(p, now, soc, nw, batt_kwh, load_fn, pv_fn, chg_cap_kw=cap, soc_log=log)
        plan.update(forecast_min_soc=round(min_soc), projected_min_soc=round(min_soc), projected_soc_window_end=None,
                    projected_soc_next_window=round(final), trace=trace[:36],
                    projected_min_at=_min_at(now, soc, log, min_soc), projection=_projection(now, soc, log))
        return plan
    ws, we = win
    lead = dt.timedelta(minutes=p.window_exit_lead_min)
    stop_at = we - lead                  # every hold and grid charge ends here, so the inverter is normal by we
    horizon = cal.next_window_start(we) or we + dt.timedelta(hours=24)
    plan.update(in_sop=True, window=f'{ws:%H:%M}-{we:%H:%M}', window_end=we.isoformat(timespec='minutes'),
                actions_stop=stop_at.isoformat(timespec='minutes'),
                horizon=horizon.isoformat(timespec='minutes'), next_window=horizon.isoformat(timespec='minutes'))
    net_now_kw = (pv_fn(now) * p.pv_margin - load_fn(now) * p.load_margin) / 1000
    plan['net_now_kw'] = round(net_now_kw, 2)
    # standby is only ever actuated while no PV is expected or measured: standby stops PV
    # harvesting and blinds the planner
    night = pv_fn(now) < p.hold_max_pv_w and pv_fn(now + dt.timedelta(minutes=30)) < p.hold_max_pv_w \
        and (pv_now_w is None or pv_now_w < p.hold_max_pv_w)
    plan['night'] = night
    step = dt.timedelta(seconds=p.sim_step_s)
    hold_end = stop_at
    t = now
    while t < hold_end:
        if pv_fn(t) >= p.hold_max_pv_w:
            hold_end = t
            break
        t += step
    hold_possible = hold_enabled and hold_end > now
    t, load_kwh, pv_kwh, pv_win_kwh = now, 0.0, 0.0, 0.0
    while t < horizon:
        load_kwh += load_fn(t) / 4000
        pv_kwh += pv_fn(t) / 4000
        if t < we:
            pv_win_kwh += pv_fn(t) / 4000
        t += dt.timedelta(minutes=15)
    plan.update(forecast_load_kwh=round(load_kwh, 1), forecast_pv_kwh=round(pv_kwh, 1),
                pv_in_window_kwh=round(pv_win_kwh, 1))

    soc_log0 = []
    min_soc0, _trace0, _, _ = simulate(p, now, soc, horizon, batt_kwh, load_fn, pv_fn, soc_log=soc_log0,
                                       chg_cap_kw=cap)
    plan['forecast_min_soc'] = round(min_soc0)

    def soc_no_charge_at(t):
        s = soc
        for ts, v in soc_log0:
            if ts > t:
                break
            s = v
        return s

    def sim(**kw):
        return simulate(p, now, soc, horizon, batt_kwh, load_fn, pv_fn, chg_cap_kw=cap, **kw)

    def clipped_with(hold_from, hold_until):
        return simulate(p, now, soc, max(horizon, now + dt.timedelta(hours=24)), batt_kwh, load_fn, pv_fn,
                        hold_from=hold_from, hold_until=hold_until, chg_cap_kw=cap)[2]

    def project(charge_to=None, charge_from=None, hold_from=None, hold_until=None):
        kw = dict(charge_to=charge_to, charge_kw=charge_kw, we=stop_at, hold_from=hold_from, hold_until=hold_until,
                  charge_from=charge_from)
        log = []
        m, tr, _, final = sim(soc_log=log, **kw)
        at_we = simulate(p, now, soc, we, batt_kwh, load_fn, pv_fn, chg_cap_kw=cap, **kw)[3]
        plan.update(projected_min_soc=round(m), projected_soc_window_end=round(at_we),
                    projected_soc_next_window=round(final), trace=tr[:36],
                    projected_min_at=_min_at(now, soc, log, m), projection=_projection(now, soc, log))

    def set_hold(hold_from, hold_until):
        plan.update(hold_start=hold_from.isoformat(timespec='minutes'), hold_end=hold_until.isoformat(timespec='minutes'),
                    hold=night and hold_from <= now < hold_until)

    if now >= stop_at:
        plan.update(action='off', target_soc=None, grid_kwh=0.0, window_closing=True,
                    reason=f'super off-peak ends at {we:%H:%M}: holds and grid charges stop at {stop_at:%H:%M} '
                           f'so the inverter is back to normal in time')
        project()
        return plan

    hyst = p.hyst_soc if (currently_on or currently_holding) else 0
    if min_soc0 >= p.reserve_soc + hyst:
        plan.update(action='off', target_soc=None, grid_kwh=0.0,
                    reason=f'no hold or grid charge needed: forecast min SOC {min_soc0:.0f}% >= reserve {p.reserve_soc:.0f}%')
        project()
        return plan

    hold_from = hold_until = None
    if hold_possible:
        d = step
        while True:
            hf = max(now, hold_end - d)
            if sim(hold_from=hf, hold_until=hold_end)[0] >= p.reserve_soc:
                clipped = clipped_with(hf, hold_end)
                plan['clipped_kwh_if_hold'] = round(clipped, 2)
                if clipped < p.hold_clip_kwh:
                    hold_from, hold_until = hf, hold_end
                break
            if hf <= now:
                break
            d += step
    if hold_from is not None:
        if hold_from - now <= (dt.timedelta(minutes=30) if currently_holding else step):
            hold_from = now
        set_hold(hold_from, hold_until)
        plan.update(action='off', target_soc=None, grid_kwh=0.0,
                    reason=f'standby hold {hold_from:%H:%M}-{hold_until:%H:%M} preserves SOC '
                           f'(min SOC without it {min_soc0:.0f}%)')
        project(hold_from=hold_from, hold_until=hold_until)
        return plan

    if hold_possible and clipped_with(now, hold_end) < p.hold_clip_kwh:
        hold_from, hold_until = now, hold_end
    # Already below the reserve: no target can keep the whole run above it, because it starts below.  Charge now
    # (no late start that would let it sink further) and judge a target by the SOC from the stop time to the horizon,
    # which is what the charge controls.  Without this every target failed and the charge went to max_soc.
    below = soc < p.reserve_soc
    jit = not currently_on and not below
    rate = max(min(charge_kw, cap), 0.3)

    def schedule(L):
        def need(start):
            kwh = max(0.0, (L - max(soc, soc_no_charge_at(start))) / 100 * batt_kwh) / p.chg_eff
            return kwh, kwh / rate
        start = now
        kwh, hours = need(start)
        if jit:
            for _ in range(5):
                nxt = max(now, stop_at - dt.timedelta(hours=hours + p.jit_margin_h))
                if abs((nxt - start).total_seconds()) < 60:
                    break
                start = nxt
                kwh, hours = need(start)
        log = []
        m = sim(charge_to=L, charge_kw=charge_kw, we=stop_at, hold_from=hold_from, hold_until=hold_until,
                charge_from=start, soc_log=log)[0]
        if below:
            m = min((v for t, v in log if t > stop_at), default=m)
        return kwh, hours, start, m

    target = None
    for L in range(int(math.ceil(soc)) + 1, int(p.max_soc) + 1):
        grid_kwh, hours_needed, start_at, m = schedule(L)
        if m >= p.reserve_soc:
            target = L
            break
    if target is None:
        target = int(p.max_soc)
        grid_kwh, hours_needed, start_at, m = schedule(target)
        plan['warning'] = f'even charging to {target}% forecasts min SOC {m:.0f}% < reserve'
    plan.update(target_soc=target, grid_kwh=round(grid_kwh, 2), hours_needed=round(hours_needed, 2),
                start_at=start_at.isoformat(timespec='minutes'), charge_end=stop_at.isoformat(timespec='minutes'))
    hold_note = ''
    if hold_until is not None:
        hold_until = min(hold_until, start_at - lead)
        if hold_until <= hold_from:
            hold_from = hold_until = None
        else:
            set_hold(hold_from, hold_until)
            hold_note = f' | standby hold {hold_from:%H:%M}-{hold_until:%H:%M} first'
    if currently_on:
        plan.update(action='on', reason=f'charging to {target}% (min SOC without it {min_soc0:.0f}%)')
    elif start_at <= now:
        plan.update(action='on', reason=f'start: need ~{grid_kwh:.1f} kWh ({hours_needed:.1f} h) to reach {target}% '
                                        f'before {stop_at:%H:%M}; min SOC otherwise {min_soc0:.0f}%')
    else:
        plan.update(action='wait', reason=f'wait; start ~{start_at:%H:%M} to reach {target}% '
                                          f'({grid_kwh:.1f} kWh) by {stop_at:%H:%M}' + hold_note)
    if plan['action'] == 'on':
        plan['hold'] = False
        project(charge_to=target)
    else:
        project(charge_to=target, charge_from=start_at, hold_from=hold_from, hold_until=hold_until)
    return plan


def plan_actions(plan: dict, preview: bool) -> list[dict]:
    out = []
    if plan.get('hold_start') and plan.get('hold_end'):
        out.append({'kind': 'hold', 'start': plan['hold_start'], 'end': plan['hold_end'], 'preview': preview})
    if plan.get('target_soc') is not None and plan.get('start_at') and plan.get('charge_end'):
        out.append({'kind': 'charge', 'start': plan['start_at'], 'end': plan['charge_end'],
                    'target': plan['target_soc'], 'preview': preview})
    return out


def _interp(points, ts):
    for (ta, va), (tb, vb) in zip(points, points[1:]):
        if ta <= ts <= tb:
            return va + (vb - va) * (ts - ta) / ((tb - ta) or 1)
    return points[-1][1] if ts > points[-1][0] else points[0][1]


def rolling_projection(p, cal, now, soc, batt_kwh, load_fn, pv_fn, charge_kw, plan, chg_cap_kw=None, hours=None):
    """Extend plan['projection'] to `hours` (default p.projection_hours).  After the decision's own horizon,
    each later super-off-peak window gets a preview decision made from the projected SOC at its start
    (no hold or charge in progress); between windows nothing is actuated.  Returns the fields to merge
    into the plan: projection, trace (hourly, same layout as simulate), actions, lowest_24h(_at)."""
    hours = p.projection_hours if hours is None else hours
    end = now + dt.timedelta(hours=hours)
    end_ts = end.timestamp()
    def cut(pts, boundary_ts):
        """Simulations step in whole sim_step_s from their start, so a segment overshoots its boundary by up
        to one step.  End it exactly at the boundary so the next preview decides from the real window start."""
        if pts[-1][0] <= boundary_ts:
            return pts
        kept = [pt for pt in pts if pt[0] < boundary_ts]
        kept.append([round(boundary_ts), round(_interp(pts, boundary_ts), 1)])
        return kept

    points = [list(pt) for pt in (plan.get('projection') or [[round(now.timestamp()), round(soc, 1)]])]
    first_end = plan.get('horizon') or plan.get('next_window')
    if first_end:
        points = cut(points, dt.datetime.fromisoformat(first_end).timestamp())
    actions = plan_actions(plan, preview=False)
    previews = []
    for _ in range(8):
        last_ts, last_soc = points[-1]
        if last_ts >= end_ts - 1:
            break
        t = dt.datetime.fromtimestamp(last_ts, cal.tz)
        if cal.current_window(t) is None:
            nxt = cal.next_window_start(t)
            seg_end = min(nxt, end) if nxt else end
            log = []
            simulate(p, t, last_soc, seg_end, batt_kwh, load_fn, pv_fn, chg_cap_kw=chg_cap_kw, soc_log=log)
            if not log:
                break
            points.extend([round(ts.timestamp()), round(v, 1)] for ts, v in log)
            points = cut(points, seg_end.timestamp())
            continue
        preview = decide(p, cal, t, last_soc, batt_kwh, load_fn, pv_fn, charge_kw, False, None, None, False, chg_cap_kw)
        seg = preview.get('projection') or []
        if len(seg) < 2 or seg[-1][0] <= last_ts:
            break
        previews.append(t.isoformat(timespec='minutes'))
        points.extend(list(pt) for pt in seg[1:])
        if preview.get('horizon'):
            points = cut(points, dt.datetime.fromisoformat(preview['horizon']).timestamp())
        actions.extend(plan_actions(preview, preview=True))
    points = cut(points, end_ts)
    actions = [a for a in actions if dt.datetime.fromisoformat(a['start']).timestamp() < end_ts]
    trace = []
    for i in range(int(hours) + 1):
        ts = now.timestamp() + i * 3600
        if ts > points[-1][0] + 1:
            break
        t = dt.datetime.fromtimestamp(ts, cal.tz)
        trace.append((t.strftime('%a %H:%M'), round(load_fn(t) * p.load_margin / 1000, 2),
                      round(pv_fn(t) * p.pv_margin / 1000, 2), round(_interp(points, ts))))
    low = min(points, key=lambda r: r[1])
    return {'projection': points, 'trace': trace, 'actions': actions, 'previewed_windows': previews,
            'lowest_24h': round(low[1]),
            'lowest_24h_at': dt.datetime.fromtimestamp(low[0], cal.tz).isoformat(timespec='minutes'),
            'projection_end': dt.datetime.fromtimestamp(points[-1][0], cal.tz).isoformat(timespec='minutes')}
