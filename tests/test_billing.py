"""Tariff, billing cycle, headline numbers, grid energy history and manual charging."""
import asyncio
import datetime as dt
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from solar01 import config
from solar01.hub import tariff
from solar01.hub.history import History
from solar01.hub.planner import model
from solar01.hub.planner.calendar import TouCalendar
from solar01.hub.planner.planner import Planner
from solar01.hub.summary import headline
from solar01.hub.web import create_app
from tests.test_planner import NORMAL, FakeCore, FakeStore

CAL = TouCalendar('America/Los_Angeles')
TZ = CAL.tz
ON_PEAK = '16-21'


def D(*a):
    return dt.datetime(*a, tzinfo=TZ)


def ts(*a):
    return int(D(*a).timestamp())


RATES = {'super_off_peak': 0.1, 'off_peak': 0.3, 'on_peak': 0.5}
TIERS = [{'up_to_kwh': 10, 'adder': -0.05}, {'up_to_kwh': None, 'adder': 0}]


class TariffTest(unittest.TestCase):
    def test_cycle_bounds(self):
        B = tariff.cycle_bounds
        self.assertEqual(B(dt.date(2026, 9, 27), 5), (dt.date(2026, 9, 5), dt.date(2026, 10, 5)))
        self.assertEqual(B(dt.date(2026, 9, 5), 5), (dt.date(2026, 9, 5), dt.date(2026, 10, 5)))
        self.assertEqual(B(dt.date(2026, 9, 4), 5), (dt.date(2026, 8, 5), dt.date(2026, 9, 5)))
        self.assertEqual(B(dt.date(2026, 1, 2), 20), (dt.date(2025, 12, 20), dt.date(2026, 1, 20)))
        self.assertEqual(B(dt.date(2026, 12, 25), 20), (dt.date(2026, 12, 20), dt.date(2027, 1, 20)))
        # day 31 is clamped to the length of each month
        self.assertEqual(B(dt.date(2026, 2, 28), 31), (dt.date(2026, 2, 28), dt.date(2026, 3, 31)))
        self.assertEqual(B(dt.date(2026, 3, 30), 31), (dt.date(2026, 2, 28), dt.date(2026, 3, 31)))

    def test_validate(self):
        t = tariff.validate({'cycle_start_day': 5, 'rates': RATES, 'tiers': TIERS})
        self.assertEqual((t['cycle_start_day'], t['rates']['on_peak'], t['tiers'][0]['up_to_kwh']), (5, 0.5, 10.0))
        self.assertEqual(tariff.validate({})['tiers'], [])
        self.assertFalse(tariff.configured(tariff.validate({})))
        for bad in ({'cycle_start_day': 0}, {'cycle_start_day': 2.5}, {'rates': {'on_peak': -1}},
                    {'rates': {'on_peak': 'x'}}, {'tiers': [{'up_to_kwh': 10, 'adder': 0}]},
                    {'tiers': [{'up_to_kwh': 10, 'adder': 0}, {'up_to_kwh': 5, 'adder': 0}, {'up_to_kwh': None}]},
                    {'tiers': [{'up_to_kwh': None, 'adder': 0}, {'up_to_kwh': None, 'adder': 0}]}, []):
            with self.assertRaises(ValueError, msg=bad):
                tariff.validate(bad)

    def test_periods(self):
        per = tariff.Periods(CAL, ON_PEAK)
        self.assertEqual([per.at(D(2026, 9, 8, h)) for h in (3, 8, 12, 15, 17, 21)],     # a Tuesday
                         ['super_off_peak', 'off_peak', 'super_off_peak', 'off_peak', 'on_peak', 'off_peak'])
        self.assertEqual(per.at(D(2026, 9, 12, 12)), 'super_off_peak')                  # Saturday: 00-14
        self.assertEqual(per.at(D(2026, 9, 7, 8)), 'super_off_peak')                    # Labor Day

    def test_price_with_tiers(self):
        t = tariff.validate({'rates': RATES, 'tiers': TIERS})
        r = tariff.price([(ts(2026, 9, 8, 3), 6), (ts(2026, 9, 8, 8), 6), (ts(2026, 9, 8, 17), 2)], t,
                         tariff.Periods(CAL, ON_PEAK))
        # 0.6 + 1.8 + 1.0, with the first 10 kWh 5 cents cheaper (the second hour straddles the tier limit)
        self.assertAlmostEqual(r['cost'], 3.4 - 0.5)
        self.assertEqual((r['kwh'], r['by_tier'], r['tier']), (14, [10, 4], 1))
        self.assertAlmostEqual(r['by_period']['off_peak']['cost'], 1.8 - 4 * 0.05)
        no_tiers = tariff.price([(ts(2026, 9, 8, 17), 2)], tariff.validate({'rates': RATES}), tariff.Periods(CAL, ON_PEAK))
        self.assertAlmostEqual(no_tiers['cost'], 1.0)
        self.assertIsNone(no_tiers['tier'])


class GridHistoryTest(unittest.TestCase):
    def test_grid_energy_is_recorded_hourly(self):
        h = History(':memory:')
        t0 = ts(2026, 9, 8, 10)
        for i in range(0, 3601 + 60, 30):              # 30 s samples across one hour and into the next
            h.add_sample(t0 + i, 1000, 500, 50, grid_in_w=600, grid_out_w=0 if i < 1800 else 100)
        h.flush()
        rows = h.hourly_energy(t0, t0 + 7200)
        self.assertEqual([r[0] for r in rows], [t0, t0 + 3600])
        _, load, pv, gin, gout = rows[0]
        # the first sample only starts the clock, so the hour is 30 s short
        self.assertAlmostEqual(load, 1000 * 3570 / 3600, delta=0.5)
        self.assertAlmostEqual(gin, 600 * 3570 / 3600, delta=0.5)
        self.assertAlmostEqual(gout, 50, delta=0.5)
        h.add_sample(t0 + 3700, 1000, 0, 50, 600, 0)
        self.assertAlmostEqual(h.hourly_energy(t0 + 3600, t0 + 7200)[0][3], 600 * 130 / 3600, delta=0.5,
                               msg='the hour in progress is included')

    def test_old_hourly_schema_is_migrated_and_backfilled(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'old.db')
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE hourly (ts INTEGER PRIMARY KEY, load_wh REAL, pv_wh REAL, secs REAL, soc REAL)')
            db.execute('CREATE TABLE minute (ts INTEGER PRIMARY KEY, grid_w REAL)')
            db.execute('INSERT INTO hourly VALUES (3600, 900, 0, 3600, 50)')
            db.execute('INSERT INTO hourly VALUES (7200, 900, 0, 3600, 50)')        # no minute rows: stays unknown
            db.executemany('INSERT INTO minute VALUES (?, ?)', [(3600 + 60 * i, 1200 if i < 30 else -600) for i in range(60)])
            db.commit()
            db.close()
            h = History(path)
            self.assertEqual(h.backfill_grid(), 1)
            self.assertEqual(h.backfill_grid(), 0, 'runs once')
            rows = h.hourly_energy(0, 10000)
            self.assertEqual(rows[0][3:], (600.0, 300.0))
            self.assertEqual(rows[1][3:], (None, None))
            h.add_minute(9000, {'soc': 40})
            h.add_minute(9030, {'soc': 40, 'jk_soc': 42})
            h.flush()
            self.assertEqual(h.soc_near(9100), (9000, 42.0))
            self.assertIsNone(h.soc_near(20000))
            h.close()


class HeadlineTest(unittest.TestCase):
    def test_headline_and_cycle(self):
        h = History(':memory:')
        # cycle starts on the 5th; grid energy known only from the 13th (before that: legacy hourly rows)
        h.db.execute('INSERT INTO hourly (ts, load_wh, pv_wh, secs, soc) VALUES (?,?,?,?,?)', (ts(2026, 9, 6, 20), 2000, 0, 3600, 50))
        h.db.execute('INSERT INTO hourly (ts, load_wh, pv_wh, secs, soc) VALUES (?,?,?,?,?)', (ts(2026, 9, 1, 20), 9999, 0, 3600, 50))
        rows = [(ts(2026, 9, 13, 3), 1000, 0, 1000), (ts(2026, 9, 27, 8), 3000, 2000, 1000),
                (ts(2026, 9, 27, 17), 2000, 0, 500)]
        for t, load, pv, gin in rows:
            h.db.execute('INSERT INTO hourly VALUES (?,?,?,?,?,?,?)', (t, load, pv, 3600, 50, gin, 0))
        h.db.execute('INSERT INTO minute (ts, jk_soc) VALUES (?, ?)', (ts(2026, 9, 26, 18), 70))
        t = tariff.validate({'cycle_start_day': 5, 'rates': RATES})
        out = headline(h, CAL, ON_PEAK, t, {'pv_forecast_today_kwh': 9.5}, 55.0, D(2026, 9, 27, 18, 5))
        self.assertEqual(out['today'], {'date': '2026-09-27', 'pv_kwh': 2.0, 'load_kwh': 5.0, 'grid_kwh': 1.5,
                                        'export_kwh': 0.0, 'pv_forecast_kwh': 9.5, 'pv_forecast_tomorrow_kwh': None})
        self.assertEqual(out['soc'], {'now': 55.0, 'prior': 70.0, 'prior_ts': ts(2026, 9, 26, 18), 'delta': -15.0})
        c = out['cycle']
        self.assertEqual((c['start'], c['end'], c['day'], c['days']), ('2026-09-05', '2026-10-05', 23, 30))
        self.assertEqual((c['load_kwh'], c['grid_kwh'], c['known_load_kwh'], c['saved_kwh']), (8.0, 2.5, 6.0, 3.5))
        self.assertTrue(c['partial'])
        self.assertEqual(c['grid_since'], ts(2026, 9, 13, 3))
        # the 27th is a Sunday: 08:00 is super off-peak, 17:00 on-peak
        self.assertAlmostEqual(c['cost'], 1 * 0.1 + 1 * 0.1 + 0.5 * 0.5)
        self.assertAlmostEqual(c['home_cost'], 1 * 0.1 + 3 * 0.1 + 2 * 0.5)
        self.assertAlmostEqual(c['saved'], c['home_cost'] - c['cost'], places=2)
        self.assertIsNone(c['projected_cost'], 'fewer than 24 hours of grid data')
        bare = headline(h, CAL, ON_PEAK, tariff.validate({}), {}, None, D(2026, 9, 27, 18, 5))['cycle']
        self.assertFalse(bare['configured'])
        self.assertNotIn('cost', bare)


def state(soc, qc=0, age=2):
    return {'inverter': {'data': {'soc': soc, 'state': 0x10, 'pv_power': 0, 'pv_power_raw': 0, 'load_power': 400,
                                  'battery_power': 0, 'battery_voltage': 52.0}, 'age_s': age},
            'holding': {'values': {'0': 0x8200, '21': NORMAL, '233': qc, '234': 20 if qc else 0, '101': 140, '66': 30},
                        'age_s': 1, 'decoded': {'bms_closed_loop': True}},
            'jk': {'data': {'soc': soc, 'capacity': 280}, 'age_s': 2}, 'emulator': {}}


class ManualChargeTest(unittest.TestCase):
    def setUp(self):
        self.store, self.core = FakeStore(), FakeCore()
        self.hist = History(':memory:')
        self.pl = Planner(config.Config(), self.hist, self.store, self.core)
        self.pl.weather.fetched = time.time()
        self.store.core_state = state(40)

    def writes(self):
        return {r: v for r, v in self.core.requests[-1]['writes']} if self.core.requests else None

    def test_validation(self):
        run = asyncio.run
        with self.assertRaises(ValueError):
            run(self.pl.start_manual('soc', target_soc=30))                # already above
        with self.assertRaises(ValueError):
            run(self.pl.start_manual('soc', target_soc=101))
        with self.assertRaises(ValueError):
            run(self.pl.start_manual('time', minutes=2))
        with self.assertRaises(ValueError):
            run(self.pl.start_manual('boost'))
        self.store.core_state = state(40, age=9999)
        self.store.core_state['jk']['age_s'] = 9999
        with self.assertRaises(ValueError):
            run(self.pl.start_manual('soc', target_soc=80))                # no fresh SOC

    def test_charge_to_soc_runs_with_the_planner_off_then_stops(self):
        asyncio.run(self.pl.set_enabled(False))
        m = asyncio.run(self.pl.start_manual('soc', target_soc=80))
        self.assertEqual(m['target_soc'], 80)
        est_h = (80 - 40) / 100 * 280 * 51.2 / 1000 / 0.93 / 2.0         # learned rate defaults to 2 kW
        self.assertAlmostEqual((m['until'] - m['started']) / 3600, min(est_h * 1.5 + 0.5, 8), delta=0.01)
        self.assertEqual(self.hist.get('manual_charge')['target_soc'], 80, 'survives a restart')
        now = dt.datetime.now(TZ)
        plan = asyncio.run(self.pl.tick(now))
        self.assertEqual((plan['action'], plan['manual'], plan['target_soc']), ('on', True, 80))
        self.assertEqual(self.writes(), {233: 1, 234: 30})
        self.assertIn('manual charge', self.core.requests[-1]['desc'])
        charge = [a for a in plan['actions'] if a['kind'] == 'charge' and not a['preview']]
        self.assertEqual(charge[0]['target'], 80)
        self.assertGreaterEqual(max(v for _, v in plan['projection']), 79, 'the projection includes the charge')
        self.assertEqual(self.pl.public()['manual']['target_soc'], 80)
        # target reached: the manual charge ends and, with the planner off, the quick charge is stopped
        self.store.core_state = state(80, qc=1)
        self.pl.last_write_mono = None
        asyncio.run(self.pl.tick(now))
        self.assertIsNone(self.pl.manual)
        self.assertEqual(self.writes(), {233: 0})
        self.assertTrue(any('reached 80 %' in msg for _, msg in self.store.events))

    def test_timed_charge_and_cancel(self):
        m = asyncio.run(self.pl.start_manual('time', minutes=120))
        self.assertEqual((m['until'] - m['started'], m['target_soc']), (7200, 100))
        plan = asyncio.run(self.pl.tick(dt.datetime.now(TZ)))
        self.assertEqual(plan['charge_end'], dt.datetime.fromtimestamp(m['until'], TZ).isoformat(timespec='minutes'))
        self.assertEqual(self.writes(), {233: 1, 234: 30})
        asyncio.run(self.pl.stop_manual('cancelled', 'test'))
        self.assertIsNone(self.hist.get('manual_charge'))
        self.store.core_state = state(45, qc=1)
        self.pl.last_write_mono = None
        # the tick's own decision is the first decide() call; the rest are rolling-projection previews
        with mock.patch.object(model, 'decide', wraps=model.decide) as decide:
            plan = asyncio.run(self.pl.tick(dt.datetime.now(TZ)))
        self.assertFalse(plan.get('manual'))
        self.assertIs(decide.call_args_list[0].args[8], False, "a leftover quick charge is not the planner's own")
        with mock.patch.object(model, 'decide', wraps=model.decide) as decide:
            asyncio.run(self.pl.tick(dt.datetime.now(TZ)))
        self.assertIs(decide.call_args_list[0].args[8], True, 'only for the first decision after the manual charge')

    def test_deadline(self):
        asyncio.run(self.pl.start_manual('time', minutes=30))
        self.pl.manual['until'] = time.time() - 1
        asyncio.run(self.pl.tick(dt.datetime.now(TZ)))
        self.assertIsNone(self.pl.manual)
        self.assertTrue(any('time reached' in msg for _, msg in self.store.events))


class ApiTest(unittest.TestCase):
    def test_tariff_summary_and_charge_endpoints(self):
        async def scenario():
            cfg = config.Config()
            hist = History(':memory:')
            store = FakeStore()
            store.core_state = state(40)
            store.planner = Planner(cfg, hist, store, FakeCore())
            app = create_app(store, hist, cfg)
            async with TestClient(TestServer(app)) as c:
                r = await c.get('/api/tariff')
                self.assertEqual((await r.json())['cycle_start_day'], 1)
                r = await c.put('/api/tariff', json={'cycle_start_day': 40})
                self.assertEqual(r.status, 400)
                r = await c.put('/api/tariff', json={'cycle_start_day': 5, 'rates': RATES, 'tiers': TIERS})
                self.assertEqual(r.status, 200)
                self.assertEqual(hist.get('tariff')['rates']['on_peak'], 0.5)
                r = await c.get('/api/summary')
                body = await r.json()
                self.assertEqual(r.status, 200)
                self.assertTrue(body['cycle']['configured'])
                self.assertEqual(body['soc']['now'], 40.0)
                r = await c.post('/api/charge', json={'mode': 'soc', 'target_soc': 20})
                self.assertEqual(r.status, 400)
                self.assertIn('already', (await r.json())['error'])
                r = await c.post('/api/charge', json={'mode': 'time', 'minutes': 60})
                self.assertEqual((await r.json())['manual']['mode'], 'time')
                r = await c.delete('/api/charge')
                self.assertIsNone((await r.json())['manual'])
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
