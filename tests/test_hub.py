"""Hub tests: HA discovery compatibility, web API, and hub + core end to end on simulated devices."""
import asyncio
import tempfile
import threading
import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

from solar01 import config
from solar01.core.service import Core
from solar01.devices import sim
from solar01.hub import mqtt
from solar01.hub.history import History
from solar01.hub.planner.planner import Planner
from solar01.hub.service import Recorder
from solar01.hub.state import Store
from solar01.hub.web import create_app
from solar01.ipc import CoreClient

# unique_ids that existed in Home Assistant before the redesign (must not change)
LEGACY_IDS = {'solar01_fb21_pv_power', 'solar01_fb21_quick_charge_remaining', 'solar01_fb21_load_energy_total',
              'solar01_jkbms_soc', 'solar01_jkbms_cell_voltage_delta', 'solar01_jkbms_charge_mos_on',
              'solar01_pi_cpu_temp', 'solar01_pi_undervoltage_now', 'solar01_bmsemu_max_charge_current',
              'solar01_bmsemu_poll_age', 'solar01_bmsemu_charge_ok', 'solar01_tou_target_soc',
              'solar01_tou_projected_min_soc', 'solar01_tou_hold_end', 'solar01_tou_pv_live_scale',
              'solar01_tou_quick_charge_active', 'solar01_tou_enabled'}


class DiscoveryTest(unittest.TestCase):
    def test_unique_ids_and_topics_preserved(self):
        msgs = mqtt.discovery_messages()
        ids = {c['unique_id'] for _, c in msgs}
        self.assertTrue(LEGACY_IDS <= ids, LEGACY_IDS - ids)
        self.assertIn('solar01_fb21_ct_power_offset_target', ids)
        self.assertIn('solar01_tou_lowest_24h', ids)
        self.assertEqual(len(ids), len(msgs), 'unique ids are unique')
        by_id = {c['unique_id']: (t, c) for t, c in msgs}
        topic, c = by_id['solar01_fb21_pv_power']
        self.assertEqual(topic, 'homeassistant/sensor/solar01_fb21_pv_power/config')
        self.assertEqual((c['state_topic'], c['availability_topic']), ('solar/flexboss21/state', 'solar/solar01/availability'))
        _, c = by_id['solar01_bmsemu_charge_ok']
        self.assertEqual({a['topic'] for a in c['availability']}, {'solar/solar01/availability', 'solar/bmsemu/availability'})
        _, c = by_id['solar01_tou_enabled']
        self.assertEqual(c['command_topic'], 'solar/tou/set/enabled')
        self.assertFalse(any('set/holding' in t or c.get('command_topic', '').startswith('solar/flexboss21')
                             for t, c in msgs), 'no inverter write entities')

    def test_emulator_payload_keys(self):
        p = mqtt.emulator_payload({'max_charge_a': 100.0, 'regs': list(range(16)), 'charge_ok': True, 'polls': 3}, 1.2)
        self.assertEqual((p['max_charge_current'], p['cell_max'], p['jk_age'], p['charge_ok']), (100.0, 10, 1.2, True))


class ShutdownTest(unittest.TestCase):
    def test_open_event_stream_does_not_block_shutdown(self):
        """A browser left on the console used to hold the hub past systemd's stop timeout (SIGKILL, lost history)."""
        from solar01.hub.service import make_runner
        from aiohttp import web as aioweb

        async def scenario():
            history = History(':memory:')
            cfg = config.Config()
            store = Store(history, cfg)
            runner = make_runner(create_app(store, history, cfg))
            await runner.setup()
            site = aioweb.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(b'GET /api/stream HTTP/1.1\r\nHost: test\r\n\r\n')
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b'event: state'), 5)
            t0 = time.monotonic()
            await asyncio.wait_for(runner.cleanup(), 10)
            self.assertLess(time.monotonic() - t0, 4)
            writer.close()
        asyncio.run(scenario())


class EventReplayTest(unittest.TestCase):
    def test_backlog_replay_after_restart_is_not_recorded_twice(self):
        history = History(':memory:')
        cfg = config.Config()
        first = Store(history, cfg)
        backlog = [{'type': 'event', 'ts': 1_789_000_000.0, 'level': 'info', 'source': 'core', 'msg': 'a'},
                   {'type': 'event', 'ts': 1_789_000_010.0, 'level': 'warning', 'source': 'core', 'msg': 'b'}]
        first.on_core_message({'type': 'events', 'events': backlog})
        first.on_core_message(backlog[1])                    # live broadcast of the same event
        first.persist()
        self.assertEqual(len(history.recent_events(10)), 2)
        second = Store(history, cfg)                          # hub restarted, core replays its backlog
        second.on_core_message({'type': 'events', 'events': backlog})
        self.assertEqual(len(history.recent_events(10)), 2)
        second.on_core_message({'type': 'event', 'ts': 1_789_000_020.0, 'level': 'info', 'source': 'core', 'msg': 'c'})
        self.assertEqual([e['msg'] for e in history.recent_events(10)], ['c', 'b', 'a'])


def sim_core(tmp):
    cfg = config.from_dict({'core': {
        'socket': 'tcp://127.0.0.1:0', 'state_file': tmp + '/core-state.json', 'broadcast_s': 0.2, 'simulate': True,
        'inverter': {'poll_s': 0.2, 'holding_refresh_s': 0.5}, 'jkbms': {'poll_s': 0.2}}})
    plant = sim.SimPlant(soc=35)
    core = Core(cfg, devices={'inverter': sim.SimInverter(plant), 'jk': sim.SimJk(plant), 'emulator_serial': None})
    thread = threading.Thread(target=core.run, daemon=True)
    thread.start()
    for _ in range(200):
        if core.server.sock is not None:
            break
        time.sleep(0.02)
    return core, thread, cfg


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core, self.thread, core_cfg = sim_core(self.tmp.name)
        self.cfg = config.from_dict({'hub': {'db': self.tmp.name + '/hub.db', 'planner': {'tick_s': 1}}})
        self.cfg.core.socket = self.core.server.bound_address()

    def tearDown(self):
        self.core.stop.set()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_hub_with_core(self):
        async def scenario():
            history = History(self.cfg.hub.db)
            store = Store(history, self.cfg)
            link = CoreClient(self.cfg.core.socket, store.on_core_message, name='hub-test')
            store.core_link = link
            planner = Planner(self.cfg, history, store, link)
            planner.weather.fetched = time.time()         # no network in tests
            store.planner = planner
            store.state_listeners.append(Recorder(history, planner))
            link_task = asyncio.create_task(link.run())
            try:
                for _ in range(100):
                    if store.core_state and store.core_state['inverter']['data'] and store.core_state['jk']['data']:
                        break
                    await asyncio.sleep(0.1)
                self.assertTrue(link.connected)
                plan = await planner.tick()
                self.assertIn(plan['action'], ('off', 'on', 'wait'))
                self.assertEqual(plan['soc_source'], 'bms')
                res = await link.request({'type': 'write', 'writes': [[233, 1], [234, 20]], 'desc': 'e2e'})
                self.assertTrue(res['ok'], res)
                for _ in range(50):
                    if (store.section('holding').get('decoded') or {}).get('quick_charge'):
                        break
                    await asyncio.sleep(0.1)
                self.assertTrue(store.section('holding')['decoded']['quick_charge'])
                self.assertTrue(any('e2e' in e['msg'] for e in [
                    {'msg': m} for m in [ev[4] for ev in history.db.execute('SELECT * FROM events').fetchall()]]))
                health = store.health()
                self.assertEqual(health['core']['status'], 'ok')
                self.assertEqual(health['emulator']['status'], 'ok')

                app = create_app(store, history, self.cfg)
                async with TestClient(TestServer(app)) as client:
                    r = await client.get('/api/state')
                    self.assertEqual(r.status, 200)
                    body = await r.json()
                    self.assertTrue(body['core_connected'] and body['inverter']['data'] and body['planner']['plan'])
                    for section in ('inverter', 'holding', 'jk', 'emulator', 'ct', 'pi', 'safety', 'workers', 'health'):
                        self.assertIn(section, body, f'{section} missing from /api/state')
                    self.assertIn('enabled', body['ct'], 'CT calibration state reaches the web UI')
                    r = await client.get('/healthz')
                    self.assertEqual(r.status, 200)
                    history.add_minute(time.time() - 120, {'pv_w': 100})
                    history.add_minute(time.time() - 60, {'pv_w': 200})
                    history.flush()
                    r = await client.get('/api/history?hours=1')
                    self.assertEqual(r.status, 200)
                    self.assertIn('pv_w', await r.json())
                    r = await client.get('/api/events?limit=5')
                    self.assertTrue((await r.json())['events'])
                    r = await client.post('/api/planner', json={'enabled': 'yes'})
                    self.assertEqual(r.status, 400)
                    r = await client.post('/api/planner', json={'enabled': False})
                    self.assertEqual(r.status, 200)
                    self.assertFalse((await r.json())['enabled'])
                    r = await client.get('/api/config')
                    self.assertEqual((await r.json())['hub']['mqtt']['password'], '')
                    r = await client.get('/api/stream')
                    line = await asyncio.wait_for(r.content.readline(), 5)
                    self.assertEqual(line, b'event: state\n')
                    r.close()
                # disabling the planner released control: quick charge stopped
                for _ in range(50):
                    if not (store.section('holding').get('decoded') or {}).get('quick_charge'):
                        break
                    await asyncio.sleep(0.1)
                self.assertFalse(store.section('holding')['decoded']['quick_charge'])
            finally:
                link_task.cancel()
                await asyncio.gather(link_task, return_exceptions=True)
                history.close()
        asyncio.run(scenario())

    def test_hub_survives_core_restart(self):
        async def scenario():
            history = History(':memory:')
            store = Store(history, self.cfg)
            link = CoreClient(self.cfg.core.socket, store.on_core_message, name='hub-test')
            store.core_link = link
            task = asyncio.create_task(link.run())
            try:
                for _ in range(100):
                    if link.connected:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(link.connected)
                self.core.stop.set()
                await asyncio.to_thread(self.thread.join, 5)
                for _ in range(100):
                    if not link.connected:
                        break
                    await asyncio.sleep(0.05)
                self.assertFalse(link.connected)
                self.assertEqual(store.health()['core']['status'], 'error')
                with self.assertRaises(ConnectionError):
                    await link.request({'type': 'ping'}, timeout=1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
