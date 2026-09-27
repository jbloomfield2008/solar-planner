"""Planner tests, ported from the live-verified test_tou.py plus tests for the new planner service."""
import asyncio
import datetime as dt
import math
import time
import unittest

from solar01 import config
from solar01.hub.history import History
from solar01.hub.planner import model
from solar01.hub.planner.calendar import TouCalendar, holidays
from solar01.hub.planner.planner import Planner, load_forecast_w
from solar01.hub.planner.weather import Weather

P = config.PlannerConfig()
CAL = TouCalendar('America/Los_Angeles')
TZ = CAL.tz
KWH = 14.3


def D(*a):
    return dt.datetime(*a, tzinfo=TZ)


def decide(now, soc, load, pv, charge_kw, currently_on, **kw):
    return model.decide(P, CAL, now, soc, KWH, load, pv, charge_kw, currently_on, **kw)


def simulate(t0, soc0, horizon, batt_kwh, load, pv, **kw):
    return model.simulate(P, t0, soc0, horizon, batt_kwh, load, pv, **kw)


def load_fn(t):
    return 350.0 if t.hour < 7 or t.hour >= 22 else (1500.0 if 17 <= t.hour < 21 else 700.0)


def pv_sunny(t):
    x = (t.hour + t.minute / 60 - 13) / 3.2
    return 2000.0 * math.exp(-x * x) if 6 < t.hour < 20 else 0.0


def pv_none(t):
    return 0.0


def pv_big(t):
    return pv_sunny(t) * 2


class CalendarTest(unittest.TestCase):
    def test_holidays_and_windows(self):
        h26 = holidays(2026)
        self.assertIn(dt.date(2026, 9, 7), h26)
        self.assertIn(dt.date(2026, 2, 16), h26)
        self.assertIn(dt.date(2026, 5, 25), h26)
        self.assertIn(dt.date(2026, 11, 26), h26)
        self.assertTrue(dt.date(2026, 7, 3) in h26 and dt.date(2026, 7, 4) not in h26)
        self.assertIn(dt.date(2027, 12, 24), holidays(2027))
        self.assertTrue(CAL.is_offpeak_day(dt.date(2026, 9, 7)) and CAL.is_offpeak_day(dt.date(2026, 9, 12)))
        self.assertFalse(CAL.is_offpeak_day(dt.date(2026, 9, 8)))
        self.assertEqual([(a.hour, b.hour) for a, b in CAL.sop_windows(D(2026, 9, 8, 12), 1)], [(0, 6), (10, 14)])
        self.assertEqual([(a.hour, b.hour) for a, b in CAL.sop_windows(D(2026, 9, 7, 12), 1)], [(0, 14)])
        self.assertEqual(CAL.current_window(D(2026, 9, 7, 9, 15))[1], D(2026, 9, 7, 14))
        self.assertIsNone(CAL.current_window(D(2026, 9, 8, 15)))
        self.assertEqual(CAL.next_window_start(D(2026, 9, 8, 15)), D(2026, 9, 9, 0))
        self.assertEqual(CAL.next_window_start(D(2026, 9, 7, 14)), D(2026, 9, 8, 0))
        self.assertTrue(TouCalendar(TZ, extra_holidays=['2026-09-08']).is_offpeak_day(dt.date(2026, 9, 8)))


class DecideTest(unittest.TestCase):
    def test_a_to_f(self):
        p = decide(D(2026, 9, 7, 9, 15), 20, load_fn, pv_sunny, 2.1, currently_on=True)
        self.assertTrue(p['in_sop'] and p['action'] == 'on' and 30 <= p['target_soc'] <= 80)
        self.assertTrue(p['horizon'].startswith('2026-09-08T00:00'))
        p = decide(D(2026, 9, 8, 15), 60, load_fn, pv_sunny, 2.1, currently_on=False)
        self.assertTrue(p['action'] == 'off' and not p['in_sop'] and p['next_window'].startswith('2026-09-09T00:00'))
        p = decide(D(2026, 9, 8, 0, 30), 40, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=False)
        self.assertTrue(p['action'] == 'wait' and p['target_soc'] > 40 and not p['hold'] and 'hold_start' not in p)
        st = dt.datetime.fromisoformat(p['start_at'])
        self.assertLess(abs((D(2026, 9, 8, 5, 50) - st).total_seconds() / 3600 - (p['hours_needed'] + P.jit_margin_h)), 0.05)
        self.assertEqual(p['charge_end'], D(2026, 9, 8, 5, 50).isoformat(timespec='minutes'))
        self.assertGreaterEqual(p['projected_min_soc'], P.reserve_soc - 1)
        p = decide(D(2026, 9, 8, 0, 30), 40, load_fn, pv_none, 2.1, currently_on=True, hold_enabled=False)
        self.assertTrue(p['action'] == 'on' and p['start_at'].startswith('2026-09-08T00:30'))
        p = decide(D(2026, 9, 8, 0, 30), 90, load_fn, pv_none, 2.1, currently_on=False)
        self.assertTrue(p['action'] == 'off' and p['forecast_min_soc'] >= P.reserve_soc)
        p = decide(D(2026, 9, 12, 2, 0), 30, load_fn, pv_sunny, 2.1, currently_on=False)
        self.assertIn(p['action'], ('wait', 'off'))
        p = decide(D(2026, 9, 12, 12, 30), 45, load_fn, lambda t: pv_sunny(t) * 0.2, 2.1, currently_on=False)
        self.assertTrue(p['action'] == 'on' and p['target_soc'] > 45)
        p = decide(D(2026, 9, 8, 11, 0), 95, load_fn, pv_sunny, 2.1, currently_on=True)
        self.assertEqual(p['action'], 'off')
        m, tr, clipped, final = simulate(D(2026, 9, 8, 12), 99, D(2026, 9, 9, 0), KWH, load_fn, pv_sunny)
        self.assertTrue(max(r[3] for r in tr) <= 100 and m >= 0 and clipped > 1 and 0 <= final <= 100)
        early = simulate(D(2026, 9, 8, 0), 30, D(2026, 9, 8, 3), KWH, load_fn, pv_none, charge_to=60, charge_kw=2.1,
                         we=D(2026, 9, 8, 6))[3]
        late = simulate(D(2026, 9, 8, 0), 30, D(2026, 9, 8, 3), KWH, load_fn, pv_none, charge_to=60, charge_kw=2.1,
                        we=D(2026, 9, 8, 6), charge_from=D(2026, 9, 8, 4))[3]
        self.assertTrue(early > 55 and late < 30, (early, late))

    def test_window_closing_stops_everything(self):
        lead = int(P.window_exit_lead_min)
        t = D(2026, 9, 8, 6) - dt.timedelta(minutes=lead - 2)
        for kw in ({'currently_on': True}, {'currently_on': False, 'currently_holding': True}):
            p = decide(t, 22, load_fn, pv_none, 2.1, hold_enabled=True, **kw)
            self.assertTrue(p['window_closing'] and p['action'] == 'off' and not p['hold'], p['reason'])
            self.assertIsNone(p['target_soc'])

    def test_simulated_charge_stops_at_the_stop_time(self):
        a = simulate(D(2026, 9, 8, 5, 30), 30, D(2026, 9, 8, 6), KWH, load_fn, pv_none, charge_to=60, charge_kw=2.1,
                     we=D(2026, 9, 8, 5, 50))[3]
        b = simulate(D(2026, 9, 8, 5, 30), 30, D(2026, 9, 8, 6), KWH, load_fn, pv_none, charge_to=60, charge_kw=2.1,
                     we=D(2026, 9, 8, 6))[3]
        self.assertLess(a, b)
        expect = 30 + (2.1 * P.chg_eff * 20 / 60 - 0.35 * P.load_margin / P.dis_eff * 10 / 60) / KWH * 100
        self.assertAlmostEqual(a, expect, delta=0.05)

    def test_projections(self):
        p = decide(D(2026, 9, 12, 2, 0), 30, load_fn, pv_sunny, 2.1, currently_on=False, hold_enabled=True)
        self.assertGreaterEqual(p['projected_soc_window_end'], p['target_soc'] - 1)
        self.assertGreaterEqual(p['projected_min_soc'], P.reserve_soc - 1)
        p = decide(D(2026, 9, 8, 15), 60, load_fn, pv_sunny, 2.1, currently_on=False)
        self.assertTrue(p['projected_soc_window_end'] is None and p['projected_soc_next_window'] < 60)
        for p in (p, decide(D(2026, 9, 8, 0, 30), 45, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True)):
            lowest = min(v for _, v in p['projection'])
            self.assertLessEqual(abs(lowest - p['projected_min_soc']), 0.6, 'strip and planner agree on the minimum')
            self.assertIn('T', p['projected_min_at'])

    def test_holds(self):
        lead = int(P.window_exit_lead_min)
        p = decide(D(2026, 9, 8, 0, 30), 60, load_fn, pv_sunny, 2.1, currently_on=False, hold_enabled=True)
        self.assertTrue(p['night'] and not p['hold'] and p['action'] == 'off' and 'hold_start' not in p)
        p = decide(D(2026, 9, 8, 0, 30), 100, load_fn, pv_big, 2.1, currently_on=False, hold_enabled=True)
        self.assertTrue(not p['hold'] and p['action'] == 'off')
        p = decide(D(2026, 9, 8, 0, 30), 45, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True)
        self.assertTrue(p['action'] == 'off' and p['forecast_min_soc'] < P.reserve_soc and 'hold_start' in p)
        hs, he = dt.datetime.fromisoformat(p['hold_start']), dt.datetime.fromisoformat(p['hold_end'])
        self.assertEqual(he, D(2026, 9, 8, 6) - dt.timedelta(minutes=lead))
        self.assertTrue(D(2026, 9, 8, 1, 0) < hs < he and not p['hold'])
        self.assertGreaterEqual(p['projected_min_soc'], P.reserve_soc - 1)
        m_short = simulate(D(2026, 9, 8, 0, 30), 45, D(2026, 9, 8, 10), KWH, load_fn, pv_none,
                           hold_from=hs + dt.timedelta(minutes=15), hold_until=he)[0]
        self.assertLess(m_short, P.reserve_soc)
        log = []
        simulate(D(2026, 9, 8, 0, 30), 45, D(2026, 9, 8, 10), KWH, load_fn, pv_none, soc_log=log)

        def soc_at(t):
            return [v for ts, v in log if ts <= t][-1]
        t1 = hs + dt.timedelta(minutes=1)
        p = decide(t1, soc_at(t1), load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True)
        self.assertTrue(p['hold'] and p['action'] == 'off' and p['hold_start'] == t1.isoformat(timespec='minutes'))
        t2 = hs + dt.timedelta(minutes=30)
        p = decide(t2, soc_at(t1) + 0.5, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True,
                   currently_holding=True)
        self.assertTrue(p['hold'] and p['hold_start'] == t2.isoformat(timespec='minutes'))
        p = decide(D(2026, 9, 8, 4, 0), 70, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True,
                   currently_holding=True)
        self.assertTrue(not p['hold'] and p['action'] == 'off')
        p = decide(D(2026, 9, 12, 12, 30), 50, load_fn, pv_sunny, 2.1, currently_on=False, hold_enabled=True)
        self.assertTrue(not p['night'] and not p['hold'] and p['net_now_kw'] > 0)
        p = decide(D(2026, 9, 12, 8, 30), 50, load_fn, lambda t: pv_sunny(t) * 0.1, 2.1, currently_on=False,
                   hold_enabled=True, pv_now_w=500)
        self.assertTrue(not p['night'] and not p['hold'])
        p = decide(D(2026, 9, 12, 7, 15), 50, load_fn, pv_sunny, 2.1, currently_on=False, hold_enabled=True)
        self.assertTrue(not p['night'] and not p['hold'])
        p = decide(D(2026, 9, 12, 6, 40), 95, load_fn, pv_sunny, 2.1, currently_on=False, hold_enabled=True)
        self.assertTrue(p['night'] and not p['hold'] and p['action'] == 'off')
        p = decide(D(2026, 9, 12, 2, 0), 25, load_fn, pv_sunny, 2.1, currently_on=False, hold_enabled=True)
        if 'hold_end' in p:
            he = dt.datetime.fromisoformat(p['hold_end'])
            self.assertTrue(he <= D(2026, 9, 12, 7, 30) and
                            pv_sunny(he) >= P.hold_max_pv_w > pv_sunny(he - dt.timedelta(minutes=15)))
        p = decide(D(2026, 9, 8, 0, 30), 22, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True)
        self.assertTrue(p['action'] == 'wait' and p['hold'] and p['hold_start'].startswith('2026-09-08T00:30'))
        st, he = dt.datetime.fromisoformat(p['start_at']), dt.datetime.fromisoformat(p['hold_end'])
        self.assertTrue(he <= st - dt.timedelta(minutes=lead) and st < D(2026, 9, 8, 6))
        self.assertGreaterEqual(p['projected_min_soc'], P.reserve_soc - 1)
        p = decide(st, 22, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True, currently_holding=True)
        self.assertTrue(p['action'] == 'on' and not p['hold'])
        a = decide(D(2026, 9, 8, 0, 30), 22, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=False)['target_soc']
        b = decide(D(2026, 9, 8, 0, 30), 22, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True)['target_soc']
        self.assertLess(b, a)

    def test_hold_lead(self):
        lead = int(P.window_exit_lead_min)
        p = decide(D(2026, 9, 8, 2, 0), 45, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True)
        self.assertTrue(p['action'] == 'off' and
                        p['hold_end'] == (D(2026, 9, 8, 6) - dt.timedelta(minutes=lead)).isoformat(timespec='minutes'))
        p = decide(D(2026, 9, 8, 6, 0) - dt.timedelta(minutes=lead - 1), 45, load_fn, pv_none, 2.1,
                   currently_on=False, hold_enabled=True, currently_holding=True)
        self.assertFalse(p['hold'])
        p = decide(D(2026, 9, 8, 2, 0), 40, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True)
        st, he = dt.datetime.fromisoformat(p['start_at']), dt.datetime.fromisoformat(p['hold_end'])
        self.assertTrue(p['action'] == 'wait' and p['hold'] and he == st - dt.timedelta(minutes=lead)
                        and st < D(2026, 9, 8, 6))

    def test_jit_energy(self):
        load_heavy = lambda t: 350.0 if t.hour < 7 else (800.0 if 17 <= t.hour < 22 else 700.0)  # noqa: E731
        p = decide(D(2026, 9, 8, 11, 30), 45, load_heavy, pv_sunny, 2.1, currently_on=False)
        self.assertTrue(p['action'] == 'wait' and p['projected_min_soc'] >= P.reserve_soc)
        naive = (p['target_soc'] - 45) / 100 * KWH / P.chg_eff
        log = []
        simulate(D(2026, 9, 8, 11, 30), 45, D(2026, 9, 8, 14), KWH, load_heavy, pv_sunny, soc_log=log)
        start = dt.datetime.fromisoformat(p['start_at'])
        soc_start = max([45] + [v for ts, v in log if ts <= start])
        expect = max(0.0, (p['target_soc'] - soc_start) / 100 * KWH / P.chg_eff)
        # PV adds charge before the late start, so the grid share is below the naive figure and matches the
        # PV-only state of charge at the start time
        self.assertTrue(p['grid_kwh'] < naive and abs(p['grid_kwh'] - expect) < 0.05, (p['grid_kwh'], expect, naive))
        p = decide(D(2026, 9, 8, 11, 30), 45, load_heavy, pv_sunny, 2.1, currently_on=True)
        self.assertLess(abs(p['grid_kwh'] - (p['target_soc'] - 45) / 100 * KWH / P.chg_eff), 0.01)

    def test_charge_duration_uses_capped_rate(self):
        """A learned rate above the battery charge cap must not shorten the planned charge."""
        a = decide(D(2026, 9, 8, 0, 30), 40, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=False)
        b = decide(D(2026, 9, 8, 0, 30), 40, load_fn, pv_none, 4.9, currently_on=False, hold_enabled=False,
                   chg_cap_kw=2.1)
        self.assertEqual((a['start_at'], a['hours_needed']), (b['start_at'], b['hours_needed']))


def hour_ts(y, m, d, hh):
    return int(D(y, m, d, hh).timestamp())


class HistoryWeatherTest(unittest.TestCase):
    def test_profile_shrinkage(self):
        h = History(':memory:')
        for ts, w in [(hour_ts(2026, 9, 4, 14), 1400.0), (hour_ts(2026, 9, 5, 14), 700.0),
                      (hour_ts(2026, 9, 6, 14), 700.0), (hour_ts(2026, 9, 7, 14), 700.0)]:
            h.db.execute('INSERT INTO hourly VALUES (?,?,?,?,?)', (ts, w, 0.0, 3600.0, 50.0))
        prof = h.load_profile(hour_ts(2026, 9, 8, 12), CAL, P.history_days, P.profile_prior_days)
        allmean = (1400 + 700 * 3) / 4
        self.assertAlmostEqual(prof['byhour'][14], allmean)
        self.assertAlmostEqual(prof['bytype'][('wd', 14)], (1400 + 2 * allmean) / 3)
        self.assertAlmostEqual(prof['bytype'][('we', 14)], (2100 + 2 * allmean) / 5)
        self.assertAlmostEqual(load_forecast_w(prof, D(2026, 9, 8, 14, 20), CAL), (1400 + 2 * allmean) / 3)
        self.assertAlmostEqual(load_forecast_w(prof, D(2026, 9, 8, 3), CAL), prof['global'])

    def test_live_scale(self):
        h = History(':memory:')
        w = Weather(P, config.SiteConfig())
        w.k = 2.0
        for hh in (9, 10, 11, 12, 13):
            w.ghi[hour_ts(2026, 9, 8, hh)] = 500.0
        h.db.execute('INSERT INTO hourly VALUES (?,?,?,?,?)', (hour_ts(2026, 9, 8, 9), 500.0, 1200.0, 3600.0, 50.0))
        h.db.execute('INSERT INTO hourly VALUES (?,?,?,?,?)', (hour_ts(2026, 9, 8, 10), 500.0, 1300.0, 3600.0, 50.0))
        h.bucket = [hour_ts(2026, 9, 8, 11), 300.0, 700.0, 1800.0, 50.0]
        now_ts = hour_ts(2026, 9, 8, 11) + 1800
        exp = (1200 + 1300 + 700) / (1000 + 1000 + 500)
        self.assertAlmostEqual(w.live_scale(h, now_ts), exp)
        now_dt = D(2026, 9, 8, 11, 30)
        self.assertAlmostEqual(w.pv_at(D(2026, 9, 8, 12), now_dt), 1000 * (1 + (exp - 1) * (1 - 0.5 / P.live_pv_fade_h)))
        self.assertEqual(w.live_scale(h, now_ts, valid=False), 1.0)
        w.ghi[hour_ts(2026, 9, 8, 9)] = 5000.0
        self.assertEqual(w.live_scale(h, now_ts), P.live_pv_min)
        self.assertEqual(w.live_scale(History(':memory:'), now_ts), 1.0)

    def test_backfill_minutes(self):
        h = History(':memory:')
        h.db.execute('INSERT INTO hourly VALUES (?,?,?,?,?)', (7200, 500.0, 1000.0, 3600.0, 55.0))
        h.db.execute('INSERT INTO hourly VALUES (?,?,?,?,?)', (10800, 600.0, 0.0, 3600.0, 50.0))
        h.add_minute(11000, {'pv_w': 1})
        h.add_minute(11030, {'pv_w': 1})
        h.flush()                                   # minute recording began inside the 10800 hour
        self.assertEqual(h.backfill_minutes(), 1)   # only the hour that ended before recording began
        s = h.series(0, 20000, 60)
        self.assertEqual((s['t'][0], s['pv_w'][0], s['soc'][0], s['jk_soc'][0]), (9000, 1000, 55, None))
        self.assertEqual(h.backfill_minutes(), 0, 'runs once')

    def test_minute_schema_migration(self):
        import os
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'old.db')
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE minute (ts INTEGER PRIMARY KEY, pv_w REAL, load_w REAL, batt_w REAL, grid_w REAL, '
                       'soc REAL, jk_soc REAL, batt_v REAL, cell_min REAL, cell_max REAL, temp_c REAL)')
            db.execute('INSERT INTO minute (ts, pv_w) VALUES (60, 5)')
            db.commit()
            db.close()
            h = History(path)
            h.add_minute(120, {'ct_offset_w': -12.5, 'load_w': 700})
            h.add_minute(150, {'ct_offset_w': -12.5, 'load_w': 700})
            h.flush()
            s = h.series(0, 1000, 60)
            self.assertEqual((s['t'], s['ct_offset_w'], s['pv_w'][0]), ([60, 120], [None, -12.5], 5))
            h.close()

    def test_weather_parse_and_cache(self):
        w = Weather(P, config.SiteConfig())
        w.parse({'time': ['2026-09-08T12:00', '2026-09-08T13:00'], 'shortwave_radiation': [800, None],
                 'cloud_cover': [10, 20], 'temperature_2m': [25.5, 26.0]})
        self.assertEqual(w.ghi_at(D(2026, 9, 8, 12, 40)), 800)
        self.assertEqual(w.ghi_at(D(2026, 9, 8, 13, 10)), 0.0)
        w2 = Weather(P, config.SiteConfig())
        w2.load_cache(__import__('json').loads(__import__('json').dumps(w.to_cache())))
        self.assertEqual(w2.ghi_at(D(2026, 9, 8, 12)), 800)

    def test_minute_series_events_and_legacy_import(self):
        import os
        import sqlite3
        import tempfile
        h = History(':memory:')
        base = 1_789_000_020.0
        for i in range(0, 130, 5):
            h.add_minute(base + i, {'pv_w': 1000 + i, 'soc': 50, 'load_w': None})
        h.flush()
        s = h.series(base - 60, base + 3600, 60)
        self.assertEqual(len(s['t']), 3, 'two complete minutes + the partial minute written by flush')
        self.assertAlmostEqual(s['pv_w'][1], 1000 + (60 + 115) / 2, delta=1)   # samples at +60..+115 s
        self.assertIsNone(s['load_w'][0])
        h.add_event(base, 'warning', 'core', 'hello', {'x': 1})
        self.assertEqual(h.recent_events(5)[0]['data'], {'x': 1})
        with tempfile.TemporaryDirectory() as d:
            legacy = os.path.join(d, 'history.db')
            src = sqlite3.connect(legacy)
            src.execute('CREATE TABLE hourly (ts INTEGER PRIMARY KEY, load_wh REAL, pv_wh REAL, secs REAL, soc REAL)')
            src.execute('CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)')
            src.execute('INSERT INTO hourly VALUES (1, 500, 0, 3600, 50)')
            src.executemany('INSERT INTO kv VALUES (?,?)', [('enabled', 'true'), ('ac_chg_w', '4894')])
            src.commit()
            src.close()
            h2 = History(os.path.join(d, 'new.db'))
            self.assertEqual(h2.import_legacy(legacy), 1)
            self.assertEqual(h2.import_legacy(legacy), 0, 'only into an empty database')
            self.assertEqual(h2.get('ac_chg_w'), 4894)
            h2.close()


class FakeCore:
    def __init__(self):
        self.requests, self.sent = [], []

    async def request(self, msg, timeout=30):
        self.requests.append(msg)
        return {'type': 'result', 'ok': True, 'results': []}

    async def send(self, msg):
        self.sent.append(msg)


class FakeStore:
    def __init__(self):
        self.core_state = None
        self.events = []

    def link_age(self):
        return 0.0

    def add_event(self, level, msg, source='hub', **data):
        self.events.append((level, msg))


NORMAL, STANDBY = 29268, 29268 & ~0x200


class ActuatorTest(unittest.TestCase):
    def setUp(self):
        self.core, self.store = FakeCore(), FakeStore()
        self.pl = Planner(config.Config(), History(':memory:'), self.store, self.core)
        self.now = D(2026, 9, 8, 0, 30)
        self.base = {'action': 'on', 'target_soc': 60, 'soc': 40.0,
                     'window_end': D(2026, 9, 8, 6).isoformat(timespec='minutes'),
                     'charge_end': D(2026, 9, 8, 5, 50).isoformat(timespec='minutes'), 'hold': False}

    def act(self, holding, now=None, ac_charging=True, **plan):
        self.pl.last_write_mono, self.pl.pending_until = None, 0.0
        n = len(self.core.requests)
        asyncio.run(self.pl.actuate(dict(self.base, **plan), now or self.now, holding, 1.0, ac_charging))
        return {r: v for r, v in self.core.requests[-1]['writes']} if len(self.core.requests) > n else None

    def test_quick_charge(self):
        self.assertEqual(self.act({233: 0, 234: 0, 21: NORMAL}), {233: 1, 234: 30})
        self.assertIsNone(self.act({233: 1, 234: 25, 21: NORMAL}), 'armed above the re-arm threshold')
        self.assertEqual(self.act({233: 1, 234: 8, 21: NORMAL}), {234: 30})
        self.assertEqual(self.act({233: 1, 234: 8, 21: NORMAL}, now=D(2026, 9, 8, 5, 40)), {234: 10})  # armed only to 05:50
        self.assertEqual(self.act({233: 1, 234: 8, 21: NORMAL}, now=D(2026, 9, 8, 5, 49, 30)), {233: 0})
        self.assertIn('super off-peak ending', self.core.requests[-1]['desc'])
        self.assertEqual(self.act({233: 1, 234: 8, 21: NORMAL}, soc=60.0), {233: 0})
        self.assertIn('reached target', self.core.requests[-1]['desc'])
        self.assertEqual(self.act({233: 1, 234: 8, 21: NORMAL}, action='off'), {233: 0})
        self.assertIsNone(self.act({233: 0, 234: 0, 21: NORMAL}, action='off'))
        self.assertEqual(self.act({233: 0x1002, 234: 0, 21: NORMAL})[233], 0x1003)

    def test_standby(self):
        self.assertEqual(self.act({233: 0, 234: 0, 21: NORMAL}, action='off', hold=True), {21: STANDBY})
        self.assertEqual(self.core.sent[-1], {'type': 'assert_standby'})
        sent = len(self.core.sent)
        self.assertIsNone(self.act({233: 0, 234: 0, 21: STANDBY}, action='off', hold=True))
        self.assertEqual(len(self.core.sent), sent + 1, 'hold asserted every tick')
        self.assertEqual(self.act({233: 1, 234: 20, 21: NORMAL}, action='wait', hold=True), {233: 0, 21: STANDBY})
        w = self.act({233: 0, 234: 0, 21: STANDBY})
        self.assertEqual(list(w), [21, 233, 234])
        self.assertEqual(w[21], NORMAL)
        self.assertEqual(self.act({233: 0, 234: 0, 21: STANDBY}, action='off'), {21: NORMAL})

    def test_rate_limit_and_stale(self):
        asyncio.run(self.pl.actuate(dict(self.base), self.now, {233: 0, 234: 0, 21: NORMAL}, 1.0, True))
        n = len(self.core.requests)
        asyncio.run(self.pl.actuate(dict(self.base), self.now, {233: 0, 234: 0, 21: NORMAL}, 1.0, True))
        self.assertEqual(len(self.core.requests), n, 'no second write within write_min_interval_s')
        plan = dict(self.base)
        self.pl.last_write_mono = None
        asyncio.run(self.pl.actuate(plan, self.now, {233: 0, 21: NORMAL}, 999.0, True))
        self.assertIn('stale', plan['warning'])

    def test_disable_releases_control(self):
        self.store.core_state = {'holding': {'values': {'21': STANDBY, '233': 1, '234': 20}, 'age_s': 1}}
        asyncio.run(self.pl.set_enabled(False))
        self.assertEqual({r: v for r, v in self.core.requests[-1]['writes']}, {21: NORMAL, 233: 0})
        self.assertFalse(self.pl.enabled)

    def test_limits_and_learned_rate(self):
        x = {'inv': {'battery_voltage': 53.0}, 'jk': {}, 'holding': {101: 140, 66: 30},
             'decoded': {'bms_closed_loop': True}}
        self.pl.ac_chg_w = 4894
        cap, rate = self.pl.limits(x)
        self.assertAlmostEqual(cap, 100 * 53.0 / 1000)     # emulator 100 A < H101 140 A
        self.assertAlmostEqual(rate, 3.0)                  # H66 = 3.0 kW
        x['decoded'] = {'bms_closed_loop': False}
        x['holding'] = {101: 40}
        cap, rate = self.pl.limits(x)
        self.assertAlmostEqual(cap, 40 * 53.0 / 1000)
        self.assertAlmostEqual(rate, cap)
        # quick charge on: battery 3500 W with 1000 W of PV surplus -> grid share 2500 W
        self.store.core_state = {'inverter': {'data': {'battery_power': 3500, 'pv_power_raw': 1500, 'load_power': 500},
                                              'age_s': 1}, 'holding': {'values': {'233': 1}, 'age_s': 1}}
        self.pl.ac_chg_w = 2000.0
        self.pl.observe(100.0)
        self.pl.observe(130.0)
        self.assertTrue(2000.0 < self.pl.ac_chg_w < 2500.0, self.pl.ac_chg_w)
        self.store.core_state['holding']['values']['233'] = 0
        before = self.pl.ac_chg_w
        self.pl.observe(160.0)
        self.assertEqual(self.pl.ac_chg_w, before, 'only learns while quick charge is on')


class RollingProjectionTest(unittest.TestCase):
    def test_full_day_outside_super_off_peak(self):
        now = D(2026, 9, 8, 15, 7)          # not on a 15-minute step: segments must still end on window starts
        plan = decide(now, 90, load_fn, pv_sunny, 2.1, currently_on=False)
        r = model.rolling_projection(P, CAL, now, 90, KWH, load_fn, pv_sunny, 2.1, plan)
        pts = r['projection']
        self.assertEqual(pts[0][0], round(now.timestamp()))
        self.assertEqual(pts[-1][0], round(now.timestamp() + 24 * 3600))
        self.assertTrue(all(b[0] > a[0] for a, b in zip(pts, pts[1:])), 'timestamps strictly increase')
        self.assertEqual(len(r['trace']), 25)
        self.assertEqual(r['previewed_windows'], [D(2026, 9, 9, 0).isoformat(timespec='minutes'),
                                                  D(2026, 9, 9, 10).isoformat(timespec='minutes')])
        self.assertIn(round(D(2026, 9, 9, 0).timestamp()), [t for t, _ in pts], 'a point exactly at the window start')
        self.assertTrue(all(a['preview'] for a in r['actions']))
        for a in r['actions']:
            start = dt.datetime.fromisoformat(a['start'])
            self.assertTrue(D(2026, 9, 9, 0) <= start < D(2026, 9, 9, 6) or D(2026, 9, 9, 10) <= start < D(2026, 9, 9, 14), a)

    def test_preview_actions_keep_the_reserve(self):
        now = D(2026, 9, 8, 23)
        plan = decide(now, 25, load_fn, pv_sunny, 2.1, currently_on=False)
        no_action = simulate(now, 25, now + dt.timedelta(hours=24), KWH, load_fn, pv_sunny)[0]
        r = model.rolling_projection(P, CAL, now, 25, KWH, load_fn, pv_sunny, 2.1, plan)
        self.assertLess(no_action, P.reserve_soc)
        self.assertTrue(r['actions'] and all(a['preview'] for a in r['actions']), r['actions'])
        self.assertGreaterEqual(r['lowest_24h'], round(no_action), 'previewed actions never make the day worse')
        night = [v for t, v in r['projection'] if D(2026, 9, 9, 0).timestamp() <= t <= D(2026, 9, 9, 10).timestamp()]
        self.assertGreaterEqual(min(night), P.reserve_soc - 1, 'the previewed night plan keeps the reserve')
        for a in r['actions']:
            end = dt.datetime.fromisoformat(a['end'])
            if a['kind'] == 'charge':
                self.assertEqual(end.minute, 50, 'charges end 10 minutes before the window closes')
            else:
                self.assertTrue(end.minute != 0, 'holds end before the window closes')

    def test_inside_a_window_keeps_the_current_actions(self):
        now = D(2026, 9, 8, 0, 30)
        plan = decide(now, 22, load_fn, pv_none, 2.1, currently_on=False, hold_enabled=True)
        r = model.rolling_projection(P, CAL, now, 22, KWH, load_fn, pv_none, 2.1, plan)
        current = [a for a in r['actions'] if not a['preview']]
        self.assertTrue(current and current[0]['kind'] == 'hold')
        self.assertTrue(r['projection'][-1][0] >= now.timestamp() + 24 * 3600 - 900)


class PlannerTickTest(unittest.TestCase):
    def test_tariff_bands(self):
        pl = Planner(config.Config(), History(':memory:'), FakeStore(), FakeCore())
        bands = pl.tariff_bands(D(2026, 9, 8, 3), D(2026, 9, 9, 3))          # Tuesday 03:00 to Wednesday 03:00
        got = [(b['kind'], dt.datetime.fromtimestamp(b['start'], TZ).hour, dt.datetime.fromtimestamp(b['end'], TZ).hour)
               for b in bands]
        self.assertEqual(got, [('super_off_peak', 3, 6), ('super_off_peak', 10, 14), ('on_peak', 16, 21),
                               ('super_off_peak', 0, 3)])
        weekend = pl.tariff_bands(D(2026, 9, 12, 0), D(2026, 9, 12, 23))
        self.assertEqual([(b['kind'], dt.datetime.fromtimestamp(b['end'], TZ).hour) for b in weekend],
                         [('super_off_peak', 14), ('on_peak', 21)])

    def test_tick_uses_bms_soc_and_actuates(self):
        store, core = FakeStore(), FakeCore()
        pl = Planner(config.Config(), History(':memory:'), store, core)
        pl.weather.fetched = time.time()
        now = D(2026, 9, 8, 0, 30)
        store.core_state = {
            'inverter': {'data': {'soc': 84, 'state': 0x10, 'pv_power': 0, 'pv_power_raw': 0, 'load_power': 400,
                                  'battery_power': -420, 'battery_voltage': 52.0}, 'age_s': 2},
            'holding': {'values': {'0': 0x8200, '21': NORMAL, '233': 0, '234': 0, '101': 140, '66': 30}, 'age_s': 5,
                        'decoded': {'bms_closed_loop': True}},
            'jk': {'data': {'soc': 15, 'capacity': 280}, 'age_s': 3}, 'emulator': {}}
        plan = asyncio.run(pl.tick(now))
        self.assertEqual(plan['soc_source'], 'bms')
        self.assertEqual(plan['soc'], 15.0)
        self.assertIn(plan['action'], ('on', 'wait', 'off'))
        self.assertTrue(len(pl.forecast) == 36 and 'load_w' in pl.forecast[0])
        store.core_state['inverter']['age_s'] = 9999
        self.assertEqual(asyncio.run(pl.tick(now))['action'], 'stale')


if __name__ == '__main__':
    unittest.main()
