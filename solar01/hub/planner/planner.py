"""Planner service: live inputs from the store, decide() in a worker thread, actuation through core.

Actuator (ported from solar_tou.py, verified live):
* Quick charge: H233 bit0 on, then H234 = minutes armed (at most qc_arm_min, never past the
  window end), re-armed when fewer than qc_rearm_below minutes remain; stopped at the target
  SOC, at the window end, or when no longer wanted.  The firmware countdown ends a charge by
  itself if the hub dies.
* Standby hold: H21 bit9 cleared while a hold is wanted; the hub asserts the hold to core every
  tick, and core's watchdog restores normal operation 20 minutes after the last assert.
* The AC Charge function (H21 bit7) is never used.

Changes from solar_tou.py:
* SOC comes from the JK BMS when fresh (the inverter's own SOC is a voltage estimate
  whenever it is not in closed-loop mode), else from the inverter.
* The battery charge cap is min(H101 charge current, emulator max charge current when in
  closed loop) x battery voltage, and the grid charge rate is the measured grid share of the
  quick-charge power (PV surplus excluded), bounded by H66 and the cap.
* Disabling the planner stops a quick charge and leaves standby straight away.

Manual charge (from the web console): a quick charge to a target SOC, or for a fixed time, that
replaces the planner's decision until it finishes, is cancelled, or hits its deadline.  It uses the
same actuator (so the same re-arming, rate limit and core safety rules), runs whether or not the
planner is enabled and at any time of day, and survives a hub restart (kept in kv 'manual_charge').
A charge to a SOC gets a deadline of 1.5 x the estimated time plus 30 minutes (1 hour to
manual_max_h), so a charge the BMS or inverter holds back cannot run on indefinitely.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import time

from ...devices.flexboss import BIT_NORMAL, BIT_QC, H_AC_CHG_POWER, H_CHG_CURRENT_A, H_FUNC, H_QC, H_QC_MINUTES, \
    MODE_AC_CHARGE
from ...util import clock_synced
from . import model
from .calendar import TouCalendar, parse_windows
from .weather import Weather

log = logging.getLogger('solar01.planner')


def load_forecast_w(profile: dict, t: dt.datetime, cal: TouCalendar) -> float:
    """Same day type and hour if seen; else any day at that hour; else the global mean."""
    key = ('we' if cal.is_offpeak_day(t.date()) else 'wd', t.hour)
    if key in profile['bytype']:
        return profile['bytype'][key]
    return profile['byhour'].get(t.hour, profile['global'])


class Planner:
    def __init__(self, cfg, history, store, core):
        self.cfg = cfg
        self.p = cfg.hub.planner
        self.cal = TouCalendar.from_config(cfg.hub.site, self.p)
        self.hist = history
        self.store = store
        self.core = core
        self.weather = Weather(self.p, cfg.hub.site)
        cache = history.get('weather_cache')
        if cache:
            try:
                self.weather.load_cache(cache)
            except (TypeError, ValueError, AttributeError):
                log.warning('ignoring unreadable weather cache')
        self.enabled = bool(history.get('enabled', self.p.enabled_default))
        self.ac_chg_w = float(history.get('ac_chg_w', self.p.ac_chg_w_default))
        self._saved_ac_chg_w = self.ac_chg_w
        self.qc_started = 0.0
        self.last_write_mono: float | None = None
        self.pending_until = 0.0
        self.last_write_desc = 'none'
        self.profile: dict | None = None
        self.profile_mono = 0.0
        self.plan: dict = {'action': 'starting', 'reason': 'waiting for the first planner tick'}
        self.forecast: list[dict] = []
        self.last_tick_mono: float | None = None
        self.last_error: str | None = None
        self._next_fetch_mono = 0.0
        self._obs_mono: float | None = None
        self.kick = asyncio.Event()
        self.manual: dict | None = history.get('manual_charge')
        self._manual_ended = False            # the running quick charge was the manual one, not the planner's

    # -- inputs ----------------------------------------------------------------------------------
    def inputs(self) -> dict | None:
        s = self.store.core_state
        if not s:
            return None
        extra = self.store.link_age()

        def section(name):
            return s.get(name) or {}

        def age(name):
            a = section(name).get('age_s')
            return None if a is None else a + extra
        return {'inv': section('inverter').get('data') or {}, 'inv_age': age('inverter'),
                'holding': {int(k): v for k, v in (section('holding').get('values') or {}).items()},
                'holding_age': age('holding'), 'decoded': section('holding').get('decoded') or {},
                'jk': section('jk').get('data') or {}, 'jk_age': age('jk'), 'emu': section('emulator')}

    def observe(self, now_mono: float | None = None) -> None:
        """Learn the grid share of the quick-charge power from each core state message."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        x = self.inputs()
        last, self._obs_mono = self._obs_mono, now_mono
        if not x or not x['inv'] or last is None:
            return
        inv = x['inv']
        if not x['holding'].get(H_QC, 0) & BIT_QC or inv.get('battery_power', 0) <= 300:
            return
        surplus = max(float(inv.get('pv_power_raw', inv.get('pv_power', 0))) - float(inv.get('load_power', 0)), 0.0)
        grid = float(inv['battery_power']) - surplus
        if grid <= 300:
            return
        alpha = 1 - math.exp(-min(now_mono - last, 30.0) / 120.0)
        self.ac_chg_w += alpha * (grid - self.ac_chg_w)

    def limits(self, x: dict) -> tuple[float, float]:
        """(battery charge cap kW, grid charge rate kW)."""
        v = float(x['inv'].get('battery_voltage') or x['jk'].get('voltage') or self.p.nominal_v)
        amps = []
        if x['holding'].get(H_CHG_CURRENT_A):
            amps.append(float(x['holding'][H_CHG_CURRENT_A]))
        if x['decoded'].get('bms_closed_loop'):
            amps.append(self.cfg.core.emulator.chg_max_a)
        cap_kw = min(amps) * v / 1000 if amps else self.p.pv_chg_max_kw
        grid_kw = self.ac_chg_w / 1000
        if x['holding'].get(H_AC_CHG_POWER):
            grid_kw = min(grid_kw, x['holding'][H_AC_CHG_POWER] / 10)
        return max(cap_kw, 0.3), max(min(grid_kw, cap_kw), 0.3)

    # -- control -------------------------------------------------------------------------------
    async def set_enabled(self, enabled: bool, source: str = 'web') -> None:
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self.hist.set('enabled', enabled)
        self.store.add_event('info', f'planner {"enabled" if enabled else "disabled"} ({source})', source='planner')
        if not enabled:
            await self.release()
        self.kick.set()

    async def release(self, desc: str = 'planner disabled: quick charge stopped / standby released') -> None:
        x = self.inputs()
        if not x:
            return
        h = x['holding']
        regs = {}
        if h.get(H_FUNC) is not None and not h[H_FUNC] & BIT_NORMAL:
            regs[H_FUNC] = h[H_FUNC] | BIT_NORMAL
        if h.get(H_QC) is not None and h[H_QC] & BIT_QC:
            regs[H_QC] = h[H_QC] & ~BIT_QC & 0xFFFF
        if regs:
            await self.write(regs, desc)

    # -- manual charge ---------------------------------------------------------------------------
    def current_soc(self, x: dict) -> float | None:
        if x['jk'].get('soc') is not None and x['jk_age'] is not None and x['jk_age'] < 60:
            return float(x['jk']['soc'])
        if x['inv'].get('soc') is not None and x['inv_age'] is not None and x['inv_age'] < self.p.stale_s:
            return float(x['inv']['soc'])
        return None

    async def start_manual(self, mode: str, target_soc=None, minutes=None, source: str = 'web') -> dict:
        """Start (or replace) a manual charge.  ValueError with a user-facing reason if it cannot start."""
        p = self.p
        x = self.inputs()
        soc = self.current_soc(x) if x else None
        if soc is None:
            raise ValueError('no fresh state of charge from the BMS or the inverter')
        now = time.time()
        if mode == 'soc':
            if isinstance(target_soc, bool) or not isinstance(target_soc, (int, float)) or not 5 <= target_soc <= p.max_soc:
                raise ValueError(f'target_soc must be 5..{p.max_soc:g}')
            target = int(round(target_soc))
            if soc >= target:
                raise ValueError(f'the battery is already at {soc:.0f} %')
            batt_kwh = p.batt_kwh or float(x['jk'].get('capacity') or 280) * p.nominal_v / 1000
            rate_kw = self.limits(x)[1]
            est_h = (target - soc) / 100 * batt_kwh / p.chg_eff / rate_kw
            until = now + min(max(est_h * 1.5 + 0.5, 1.0), p.manual_max_h) * 3600
            what = f'to {target} %'
        elif mode == 'time':
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) \
                    or not 5 <= minutes <= p.manual_max_h * 60:
                raise ValueError(f'minutes must be 5..{p.manual_max_h * 60:g}')
            target = int(p.max_soc)
            if soc >= target:
                raise ValueError(f'the battery is already at {soc:.0f} %')
            until = now + minutes * 60
            what = f'for {minutes:g} min'
        else:
            raise ValueError('mode must be "soc" or "time"')
        self.manual = {'mode': mode, 'target_soc': target, 'started': round(now), 'until': round(until),
                       'start_soc': round(soc, 1), 'source': source}
        self.hist.set('manual_charge', self.manual)
        stop = dt.datetime.fromtimestamp(until, self.cal.tz)
        self.store.add_event('info', f'manual charge {what} requested at {soc:.0f} %, stops by {stop:%H:%M} ({source})',
                             source='planner')
        self.kick.set()
        return self.manual

    async def stop_manual(self, why: str, source: str | None = None) -> None:
        if not self.manual:
            return
        self.manual = None
        self._manual_ended = True
        self.hist.set('manual_charge', None)
        self.store.add_event('info', f'manual charge ended: {why}' + (f' ({source})' if source else ''), source='planner')
        if not self.enabled:
            await self.release('manual charge ended: quick charge stopped')
        self.kick.set()

    def manual_plan(self, plan: dict, now_dt: dt.datetime, soc: float, batt_kwh: float, load_fn, pv_fn,
                    charge_kw: float, cap_kw: float) -> dict:
        """The decision replaced by the manual charge, with a projection that includes it."""
        m, p = self.manual, self.p
        until = dt.datetime.fromtimestamp(m['until'], self.cal.tz)
        target = m['target_soc']
        log: list = []
        model.simulate(p, now_dt, soc, until, batt_kwh, load_fn, pv_fn, charge_to=target, charge_kw=charge_kw, we=until,
                       charge_from=now_dt, soc_log=log, chg_cap_kw=cap_kw)
        eta = next((t for t, v in log if v >= target - 0.5), None)
        kwh = max(0.0, (target - soc) / 100 * batt_kwh / p.chg_eff)
        out = {k: v for k, v in plan.items() if k not in ('hold_start', 'hold_end', 'window_closing', 'warning')}
        what = f'to {target} %' if m['mode'] == 'soc' else f'until {until:%H:%M}'
        reached = f', expected to reach {target} % at {eta:%H:%M}' if eta and m['mode'] == 'soc' else ''
        out.update(action='on', manual=True, target_soc=target, hold=False, grid_kwh=round(kwh, 2),
                   start_at=now_dt.isoformat(timespec='minutes'), charge_end=until.isoformat(timespec='minutes'),
                   horizon=until.isoformat(timespec='minutes'), projection=model._projection(now_dt, soc, log),
                   manual_eta=eta.isoformat(timespec='minutes') if eta else None,
                   reason=f'manual charge {what}{reached}; stops by {until:%H:%M} at the latest')
        return out

    def next_boundary_s(self) -> float | None:
        """Seconds until the next planned start or stop in the current window."""
        now = dt.datetime.now(self.cal.tz)
        best = None
        for key in ('hold_start', 'hold_end', 'start_at', 'charge_end', 'actions_stop'):
            value = self.plan.get(key)
            if not value:
                continue
            try:
                seconds = (dt.datetime.fromisoformat(value) - now).total_seconds()
            except (TypeError, ValueError):
                continue
            if seconds > 0 and (best is None or seconds < best):
                best = seconds
        return best

    async def run(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception('planner tick')
                self.last_error = str(e)
            self.kick.clear()
            wait = max(1.0, self.p.tick_s - (time.monotonic() - t0))
            boundary = self.next_boundary_s()
            if boundary is not None:
                wait = max(1.0, min(wait, boundary + 1.0))     # re-decide right after a planned start or stop
            try:
                await asyncio.wait_for(self.kick.wait(), wait)
            except asyncio.TimeoutError:
                pass

    async def tick(self, now_dt: dt.datetime | None = None) -> dict:
        p, cal = self.p, self.cal
        now_dt = now_dt or dt.datetime.now(cal.tz)
        now = now_dt.timestamp()
        mono = time.monotonic()
        self.last_tick_mono = mono
        base = {'enabled': self.enabled, 'dry_run': p.dry_run, 'last_write': self.last_write_desc}
        if not clock_synced():
            self.plan = {**base, 'action': 'waiting', 'reason': 'waiting for the clock to synchronise'}
            return self.plan
        if time.time() - self.weather.fetched > p.weather_refresh_s and mono >= self._next_fetch_mono:
            if await asyncio.to_thread(self.weather.fetch):
                self.weather.calibrate(self.hist, now)
                self.hist.set('weather_cache', self.weather.to_cache())
            else:
                self._next_fetch_mono = mono + 300
        if self.profile is None or mono - self.profile_mono > 900:
            self.profile = self.hist.load_profile(now, cal, p.history_days, p.profile_prior_days)
            self.profile_mono = mono
        profile = self.profile
        x = self.inputs()
        jk = x['jk'] if x else {}
        batt_kwh = p.batt_kwh or float(jk.get('capacity') or 280) * p.nominal_v / 1000
        today = now_dt.date()
        tomorrow = today + dt.timedelta(days=1)
        w = self.weather
        plan = {**base, 'pv_k': round(w.k, 2), 'weather_ok': w.ok, 'weather_fetched': round(w.fetched) or None,
                'history_days': round(profile['days'], 1),
                'ghi_today_kwh': round(w.day_ghi_kwh(today), 2), 'ghi_tomorrow_kwh': round(w.day_ghi_kwh(tomorrow), 2),
                'pv_forecast_today_kwh': round(w.day_ghi_kwh(today) * w.k, 1),
                'pv_forecast_tomorrow_kwh': round(w.day_ghi_kwh(tomorrow) * w.k, 1),
                'holiday': cal.is_offpeak_day(today), 'in_sop': cal.current_window(now_dt) is not None,
                'charge_rate_learned_w': round(self.ac_chg_w)}
        load_fn = lambda t: load_forecast_w(profile, t, cal)             # noqa: E731
        pv_fn = lambda t: w.pv_at(t, now_dt)                              # noqa: E731
        if not x or not x['inv'] or x['inv_age'] is None or x['inv_age'] > p.stale_s:
            plan.update(action='stale', reason='no inverter data')
            self.plan = plan
            return plan
        inv, holding = x['inv'], x['holding']
        if jk.get('soc') is not None and x['jk_age'] is not None and x['jk_age'] < 60:
            soc, plan['soc_source'] = float(jk['soc']), 'bms'
        else:
            soc, plan['soc_source'] = float(inv.get('soc', 0)), 'inverter'
        if self.manual:
            m = self.manual
            if soc >= m['target_soc']:
                await self.stop_manual(f'reached {soc:.0f} % (target {m["target_soc"]} %)')
            elif now >= m['until']:
                limit = 'deadline' if m['mode'] == 'soc' else 'time'
                await self.stop_manual(f'{limit} reached at {soc:.0f} %')
        ac_charging = bool(inv.get('state', 0) & MODE_AC_CHARGE)
        qc = holding.get(H_QC)
        currently_on = bool(qc & BIT_QC) if qc is not None else ac_charging
        if self._manual_ended:
            # a quick charge still running after a manual charge is not one the planner decided on: decide afresh
            # (a charge it does not need is stopped) instead of carrying it on to the planner's own target
            currently_on, self._manual_ended = False, False
        func = holding.get(H_FUNC)
        in_standby = (not func & BIT_NORMAL) if func is not None else False
        plan.update(ac_charging=ac_charging, quick_charge_active=currently_on,
                    quick_charge_minutes=holding.get(H_QC_MINUTES), standby=in_standby if func is not None else None)
        pv_now_w = None if in_standby else float(inv.get('pv_power_raw', inv.get('pv_power', 0)))
        plan['pv_live_scale'] = round(w.live_scale(self.hist, now, valid=not in_standby), 2)
        cap_kw, charge_kw = self.limits(x)
        result = await asyncio.to_thread(model.decide, p, cal, now_dt, soc, batt_kwh, load_fn, pv_fn, charge_kw,
                                         currently_on, None, pv_now_w, in_standby, cap_kw)
        if self.manual:
            result = self.manual_plan(result, now_dt, soc, batt_kwh, load_fn, pv_fn, charge_kw, cap_kw)
        plan.update(result)
        # a full day: this decision, then a preview of what each later window would decide
        plan.update(await asyncio.to_thread(model.rolling_projection, p, cal, now_dt, soc, batt_kwh, load_fn, pv_fn,
                                            charge_kw, result, cap_kw))
        plan['now_ts'] = round(now)                   # trace row i is at now_ts + i hours
        self.forecast = [{'ts': h['ts'], 'pv_w': h['pv_w'], 'ghi': h['ghi'], 'cloud': h['cloud'], 'temp': h['temp'],
                          'load_w': round(load_fn(dt.datetime.fromtimestamp(h['ts'], cal.tz)))}
                         for h in w.hourly(now_dt, 36, now_dt)]
        if self.enabled or self.manual:
            await self.actuate(plan, now_dt, holding, x['holding_age'], ac_charging)
        else:
            plan['reason'] = 'planner disabled; ' + plan.get('reason', '')
        plan['last_write'] = self.last_write_desc
        plan['ts'] = round(time.time(), 1)
        self.plan = plan
        if abs(self.ac_chg_w - self._saved_ac_chg_w) > 25:
            self.hist.set('ac_chg_w', round(self.ac_chg_w))
            self._saved_ac_chg_w = self.ac_chg_w
        return plan

    async def actuate(self, plan: dict, now_dt: dt.datetime, holding: dict, holding_age, ac_charging: bool) -> None:
        p = self.p
        if not holding or holding_age is None or holding_age > p.stale_s:
            plan['warning'] = 'holding registers stale; not writing'
            return
        qc = holding.get(H_QC)
        if qc is None:
            plan['warning'] = 'quick-charge registers unknown; not writing'
            return
        mono = time.monotonic()
        target = plan.get('target_soc')
        want_on = plan.get('action') == 'on' and target is not None and plan['soc'] < target
        arm = p.qc_arm_min
        closing = bool(plan.get('window_closing'))
        if want_on:
            stop_iso = plan.get('charge_end') or plan.get('actions_stop') or plan['window_end']
            left = (dt.datetime.fromisoformat(stop_iso) - now_dt).total_seconds() / 60
            arm = int(max(5, min(p.qc_arm_min, math.ceil(left))))
            if left < 1:
                want_on, closing = False, True
        want_hold = bool(plan.get('hold')) and not want_on
        if want_hold:
            try:
                await self.core.send({'type': 'assert_standby'})
            except ConnectionError:
                pass
        if mono < self.pending_until or (self.last_write_mono is not None
                                         and mono - self.last_write_mono < p.write_min_interval_s):
            return
        active = bool(qc & BIT_QC)
        minutes = holding.get(H_QC_MINUTES, 0)
        func = holding.get(H_FUNC)
        normal = None if func is None else bool(func & BIT_NORMAL)
        regs: dict[int, int] = {}
        desc: list[str] = []
        if want_on:
            if normal is False:
                regs[H_FUNC] = func | BIT_NORMAL
                desc.append('standby OFF')
            if not active:
                regs[H_QC] = qc | BIT_QC
                regs[H_QC_MINUTES] = arm
                self.qc_started = mono
                desc.append(f'quick charge START {arm} min, target {target}%')
            elif minutes < p.qc_rearm_below or minutes > p.qc_arm_min:
                regs[H_QC_MINUTES] = arm
                desc.append(f'quick charge re-arm {arm} min, target {target}%')
            if active and mono - self.qc_started > 180 and not ac_charging:
                plan['warning'] = 'quick charge armed but the inverter is not charging (BMS limit? H67 SOC limit?)'
        elif want_hold:
            if active:
                regs[H_QC] = qc & ~BIT_QC & 0xFFFF
                desc.append('quick charge STOP')
            if normal:
                regs[H_FUNC] = func & ~BIT_NORMAL & 0xFFFF
                desc.append('STANDBY hold (grid feeds loads)')
        else:
            ending = ' (super off-peak ending)' if closing else ''
            if normal is False:
                regs[H_FUNC] = func | BIT_NORMAL
                desc.append('standby OFF' + ending)
            if active:
                regs[H_QC] = qc & ~BIT_QC & 0xFFFF
                if closing:
                    why = ending
                elif target is not None and plan['soc'] >= target:
                    why = f' (SOC {plan["soc"]:.0f}% reached target {target}%)'
                else:
                    why = ' (no longer needed)'
                desc.append('quick charge STOP' + why)
        if regs:
            await self.write(regs, ('manual charge: ' if plan.get('manual') else '') + ', '.join(desc), plan)

    async def write(self, regs: dict, desc: str, plan: dict | None = None) -> bool:
        self.last_write_mono = time.monotonic()
        self.pending_until = self.last_write_mono + 30
        self.last_write_desc = f'{dt.datetime.now(self.cal.tz):%m-%d %H:%M} {desc}' + (' (dry run)' if self.p.dry_run else '')
        log.info('WRITE %s -> %s%s', desc, regs, ' [DRY RUN]' if self.p.dry_run else '')
        if self.p.dry_run:
            self.store.add_event('info', f'planner (dry run): {desc}', source='planner')
            return True
        try:
            res = await self.core.request({'type': 'write', 'writes': [[r, v] for r, v in regs.items()],
                                           'desc': f'planner: {desc}'})
        except (ConnectionError, asyncio.TimeoutError, OSError) as e:
            if plan is not None:
                plan['warning'] = f'write failed: {e or type(e).__name__}'
            self.pending_until = 0.0
            return False
        self.pending_until = time.monotonic() + 3             # next core state carries the read-backs
        if not res.get('ok') and plan is not None:
            errors = [r.get('error') for r in res.get('results', []) if not r.get('ok')]
            plan['warning'] = f'write refused: {errors[0] if errors else res.get("error")}'
        return bool(res.get('ok'))

    def tariff_bands(self, start: dt.datetime, end: dt.datetime) -> list[dict]:
        """Super-off-peak and on-peak periods overlapping [start, end] (anything else is off-peak)."""
        on_peak = parse_windows(self.p.on_peak)
        out = []
        d = start.date() - dt.timedelta(days=1)
        while d <= end.date():
            day0 = dt.datetime.combine(d, dt.time(0), self.cal.tz)
            for kind, windows in (('super_off_peak', self.cal.sop_hours(d)), ('on_peak', on_peak)):
                for a, b in windows:
                    ws = dt.datetime.combine(d, dt.time(a), self.cal.tz)
                    we = day0 + dt.timedelta(hours=b)
                    if we > start and ws < end:
                        out.append({'kind': kind, 'start': round(max(ws, start).timestamp()),
                                    'end': round(min(we, end).timestamp())})
            d += dt.timedelta(days=1)
        return sorted(out, key=lambda b: b['start'])

    def public(self) -> dict:
        now_dt = dt.datetime.now(self.cal.tz)
        t0 = self.plan.get('now_ts')
        projection = self.plan.get('projection') or (
            [[t0 + i * 3600, row[3]] for i, row in enumerate(self.plan.get('trace') or [])] if t0 else [])
        return {'plan': self.plan, 'forecast': self.forecast, 'enabled': self.enabled, 'dry_run': self.p.dry_run,
                'manual': dict(self.manual, eta=self.plan.get('manual_eta')) if self.manual else None,
                'projection': projection,
                'tariff': self.tariff_bands(now_dt - dt.timedelta(hours=14), now_dt + dt.timedelta(hours=38)),
                'last_error': self.last_error,
                'last_tick_age_s': None if self.last_tick_mono is None else round(time.monotonic() - self.last_tick_mono),
                'weather': {'ok': self.weather.ok, 'fetched': round(self.weather.fetched) or None,
                            'last_error': self.weather.last_error, 'pv_k': round(self.weather.k, 2)}}
