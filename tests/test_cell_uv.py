"""Cell undervoltage standby: any cell below cell_uv_standby_mv puts the inverter in standby until the next window."""
import asyncio
import datetime as dt
import time
import unittest

from solar01 import config
from solar01.hub.history import History
from solar01.hub.planner.planner import Planner
from tests.test_billing import state
from tests.test_planner import NORMAL, STANDBY, FakeCore, FakeStore, D


def with_cells(st, mv, standby=False, jk_age=2):
    st['jk']['data']['cell_voltage_min'] = mv
    st['jk']['age_s'] = jk_age
    st['holding']['values']['21'] = STANDBY if standby else NORMAL
    return st


class CellUndervoltageTest(unittest.TestCase):
    def setUp(self):
        self.store, self.core = FakeStore(), FakeCore()
        self.hist = History(':memory:')
        self.pl = Planner(config.Config(), self.hist, self.store, self.core)
        self.pl.weather.fetched = time.time()           # no irradiance data: no PV anywhere, the house needs the grid
        self.night = D(2026, 9, 28, 22)                 # Monday, between windows

    def tick(self, now, st):
        self.store.core_state = st
        self.pl.last_write_mono, self.pl.pending_until = None, 0.0
        n = len(self.core.requests)
        plan = asyncio.run(self.pl.tick(now))
        writes = {r: v for r, v in self.core.requests[-1]['writes']} if len(self.core.requests) > n else None
        return plan, writes

    def test_standby_until_the_next_window(self):
        plan, writes = self.tick(self.night, with_cells(state(40), 2790))
        self.assertEqual((plan['hold'], plan['uv_hold'], plan['uv_cell_mv']), (True, True, 2790))
        self.assertEqual(plan['hold_end'], D(2026, 9, 29, 0).isoformat(timespec='minutes'))
        self.assertEqual(writes, {21: STANDBY})
        self.assertEqual(self.core.sent[-1], {'type': 'assert_standby'})
        self.assertTrue(any(level == 'warning' and '2790 mV' in msg for level, msg in self.store.events))
        self.assertEqual(self.hist.get('cell_uv_hold')['mv'], 2790, 'kept across a hub restart')
        # resting in standby, the cell recovers above the threshold: still latched
        plan, writes = self.tick(self.night + dt.timedelta(minutes=30), with_cells(state(40), 2950, standby=True))
        self.assertTrue(plan['uv_hold'])
        self.assertIsNone(writes)
        # a restarted hub picks the latch up
        pl2 = Planner(config.Config(), self.hist, self.store, self.core)
        self.assertEqual(pl2.uv_hold['mv'], 2790)
        # super off-peak starts: released, and the window's own decision applies
        plan, _ = self.tick(D(2026, 9, 29, 0, 5), with_cells(state(40), 2950, standby=True))
        self.assertIsNone(self.pl.uv_hold)
        self.assertFalse(plan.get('uv_hold'))
        self.assertTrue(any('super off-peak started' in msg for _, msg in self.store.events))

    def test_inside_a_window_it_holds_until_the_next_one(self):
        plan, _ = self.tick(D(2026, 9, 29, 3), with_cells(state(60), 2790))          # Tuesday 03:00, window 00-06
        if plan['action'] != 'on':
            self.assertTrue(plan['uv_hold'])
        self.assertEqual(self.pl.uv_hold['until'], int(D(2026, 9, 29, 10).timestamp()))

    def test_not_when_off_stale_or_fine(self):
        plan, writes = self.tick(self.night, with_cells(state(40), 2850))
        self.assertFalse(plan.get('uv_hold'))
        plan, _ = self.tick(self.night, with_cells(state(40), 2700, jk_age=999))
        self.assertIsNone(self.pl.uv_hold, 'stale BMS data is not acted on')
        asyncio.run(self.pl.set_enabled(False))
        plan, _ = self.tick(self.night, with_cells(state(40), 2700))
        self.assertIsNone(self.pl.uv_hold, 'the planner switch turns it off')

    def test_a_charge_takes_precedence(self):
        st = with_cells(state(40), 2790)
        self.store.core_state = st
        asyncio.run(self.pl.start_manual('soc', target_soc=80))          # its deadline runs on the real clock
        plan, writes = self.tick(dt.datetime.now(self.pl.cal.tz), st)
        self.assertEqual(plan['action'], 'on')
        self.assertFalse(plan.get('uv_hold'))
        self.assertEqual(writes, {233: 1, 234: 30})
        self.assertIsNotNone(self.pl.uv_hold, 'latched, so standby follows once the charge ends')

    def test_state_messages_kick_the_planner_once_a_minute(self):
        self.store.core_state = with_cells(state(40), 2790)
        self.pl.observe(100.0)
        self.assertTrue(self.pl.kick.is_set())
        self.pl.kick.clear()
        self.pl.observe(102.0)
        self.assertFalse(self.pl.kick.is_set(), 'not every 2 s')
        self.pl.observe(170.0)
        self.assertTrue(self.pl.kick.is_set())


if __name__ == '__main__':
    unittest.main()
