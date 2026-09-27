"""Core process tests: safety rules, CT calibration, IPC, and the whole core on simulated devices."""
import json
import socket
import tempfile
import threading
import time
import unittest

from solar01 import config
from solar01.core import safety
from solar01.core.ct import CtCalibrator
from solar01.core.service import Core
from solar01.devices import modbus
from solar01.devices import sim
from solar01.ipc import IpcServer, encode

NORMAL = 29268                       # live H21 value on this inverter
STANDBY = NORMAL & ~0x200


class SafetyTest(unittest.TestCase):
    def test_h21_only_standby_bit(self):
        self.assertIsNone(safety.check_write(21, STANDBY, NORMAL, 60))
        self.assertIsNone(safety.check_write(21, NORMAL, STANDBY, 60))
        self.assertIn('bit 9', safety.check_write(21, NORMAL | 0x80, NORMAL, 60), 'AC charge bit refused')
        self.assertIsNotNone(safety.check_write(21, 0, NORMAL, 60))
        self.assertIsNotNone(safety.check_write(21, NORMAL, None, 60))

    def test_h233_only_quick_charge_bit(self):
        self.assertIsNone(safety.check_write(233, 0x1003, 0x1002, 60))
        self.assertIsNotNone(safety.check_write(233, 0x0001, 0x1002, 60))

    def test_h234_range_and_other_registers(self):
        self.assertIsNone(safety.check_write(234, 30, None, 60))
        self.assertIsNotNone(safety.check_write(234, 4, None, 60))
        self.assertIsNotNone(safety.check_write(234, 61, None, 60))
        for reg in (0, 60, 66, 67, 101, 105, 119, 160):
            self.assertIn('not writable', safety.check_write(reg, 1, 0, 60))
        self.assertIsNotNone(safety.check_write(234, 70000, None, 60))

    def test_ct_calibration(self):
        cfg = config.InverterConfig(ct_cal_slope=-0.045, ct_cal_intercept=20, ct_cal_max_w=0)
        self.assertEqual(safety.ct_offset_target(400, cfg), 0)             # +2 W, but never bias toward export
        unclamped = config.InverterConfig(ct_cal_slope=-0.045, ct_cal_intercept=20)
        self.assertEqual(safety.ct_offset_target(400, unclamped), 20)      # 2.0 W in 0.1 W units
        self.assertEqual(safety.ct_offset_target(1000, cfg), -250)         # -25 W
        self.assertEqual(safety.ct_offset_target(0, cfg), 0)               # +20 W clamped to max 0
        self.assertEqual(safety.ct_offset_target(20000, cfg), -2500)       # firmware floor
        self.assertTrue(safety.ct_should_write(-250, None, None, cfg))
        self.assertFalse(safety.ct_should_write(-250, -100, None, cfg), 'within 20 W hysteresis')
        self.assertTrue(safety.ct_should_write(-350, -100, 61, cfg))
        self.assertFalse(safety.ct_should_write(-350, -100, 30, cfg), 'rate limited')
        self.assertFalse(safety.ct_should_write(-350, -100, None, config.InverterConfig()), 'disabled')

    def test_standby_watchdog(self):
        cfg = config.InverterConfig(standby_watchdog_s=1200)
        self.assertTrue(safety.standby_watchdog_due(STANDBY, 1201, cfg))
        self.assertFalse(safety.standby_watchdog_due(STANDBY, 600, cfg))
        self.assertFalse(safety.standby_watchdog_due(NORMAL, 5000, cfg))
        self.assertFalse(safety.standby_watchdog_due(None, 5000, cfg))

    def test_decode_h0(self):
        d = safety.decode_holding({0: 0x8200, 21: STANDBY, 233: 1, 234: 25, 101: 140, 119: -200})
        self.assertEqual((d['battery_type'], d['lithium_brand'], d['bms_closed_loop']), ('lithium', 0, True))
        self.assertTrue(d['standby'] and d['quick_charge'] and not d['ac_charge_function'])
        self.assertEqual(d['ct_offset_w'], -20.0)
        self.assertEqual(safety.decode_holding({0: 0x9500})['battery_type'], 'lead-acid')
        self.assertEqual(safety.decode_holding({0: 0x8100})['battery_type'], 'lead-acid')

    def test_ct_calibrator(self):
        cfg = config.InverterConfig(ct_cal_slope=-0.045, ct_cal_intercept=20, ct_cal_max_w=0)
        ct = CtCalibrator(cfg)
        writes = []
        ct.observe_register(200)                                   # +20 W found on the inverter
        self.assertEqual(ct.step(1000, writes.append, 100.0), 'written')
        self.assertEqual(writes, [-250])                           # -25 W
        self.assertIsNone(ct.step(1100, writes.append, 130.0))     # target -29.5 W is within 20 W
        self.assertIsNone(ct.step(2000, writes.append, 140.0))     # target -70 W, but only 40 s since the write
        st = ct.state(140.0)
        self.assertTrue(st['pending'])
        self.assertAlmostEqual(st['next_write_in_s'], 20)
        self.assertEqual(ct.step(2000, writes.append, 161.0), 'written')
        self.assertEqual(writes[-1], -700)

        def fail(_value):
            raise modbus.LinkError('no reply')
        self.assertEqual(ct.step(5000, fail, 300.0), 'failed')
        st = ct.state(300.0)
        self.assertIn('no reply', st['last_error'])
        self.assertEqual((st['offset_w'], st['writes'], st['last_write_w'], st['target_w']), (-70.0, 2, -70.0, -205.0))
        off = CtCalibrator(config.InverterConfig())
        self.assertIsNone(off.step(1000, writes.append, 1.0))
        self.assertFalse(off.state(1.0)['enabled'])


class LineClient:
    def __init__(self, address):
        host, port = address[6:].rsplit(':', 1)
        self.sock = socket.create_connection((host, int(port)), timeout=10)
        self.buf = b''

    def send(self, msg):
        self.sock.sendall(encode(msg))

    def recv(self, timeout=10):
        self.sock.settimeout(timeout)
        while b'\n' not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('closed')
            self.buf += chunk
        line, self.buf = self.buf.split(b'\n', 1)
        return json.loads(line)

    def wait_for(self, pred, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = self.recv(timeout=max(0.1, end - time.monotonic()))
            if pred(msg):
                return msg
        raise TimeoutError('message not seen')

    def close(self):
        self.sock.close()


class IpcTest(unittest.TestCase):
    def test_roundtrip_and_broadcast(self):
        got = []

        def on_message(client, msg):
            got.append(msg)
            client.send_msg({'type': 'result', 'id': msg.get('id'), 'ok': True})
        server = IpcServer('tcp://127.0.0.1:0', on_message)
        server.start()
        try:
            c = LineClient(server.bound_address())
            c.send({'type': 'write', 'id': 'x1', 'writes': [[234, 30]]})
            self.assertEqual(c.recv(), {'type': 'result', 'id': 'x1', 'ok': True})
            server.broadcast({'type': 'state', 'n': 1})
            self.assertEqual(c.recv()['n'], 1)
            c.close()
        finally:
            server.close()


class CoreSimTest(unittest.TestCase):
    """The complete core (all threads, IPC, safety rules) on simulated devices."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.from_dict({'core': {
            'socket': 'tcp://127.0.0.1:0', 'state_file': self.tmp.name + '/core-state.json', 'broadcast_s': 0.2,
            'simulate': True, 'inverter': {'poll_s': 0.2, 'holding_refresh_s': 0.5, 'standby_watchdog_s': 1.5,
                                           'ct_cal_slope': -0.045, 'ct_cal_intercept': 20, 'ct_cal_max_w': 0,
                                           'ct_cal_min_write_s': 0.5},
            'jkbms': {'poll_s': 0.2}}})
        plant = sim.SimPlant(soc=40)
        self.core = Core(cfg, devices={'inverter': sim.SimInverter(plant), 'jk': sim.SimJk(plant),
                                       'emulator_serial': None})
        self.thread = threading.Thread(target=self.core.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.core.server.sock is not None:
                break
            time.sleep(0.02)
        self.client = LineClient(self.core.server.bound_address())
        self.client.send({'type': 'hello', 'name': 'test'})

    def tearDown(self):
        self.client.close()
        self.core.stop.set()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def write(self, writes, rid):
        self.client.send({'type': 'write', 'id': rid, 'writes': writes, 'desc': 'test'})
        return self.client.wait_for(lambda m: m.get('type') == 'result' and m.get('id') == rid)

    def test_state_and_emulator(self):
        st = self.client.wait_for(lambda m: m.get('type') == 'state' and m['inverter']['data'] and m['jk']['data']
                                  and m['holding']['values'])
        self.assertEqual(st['inverter']['data']['soc'], 40)
        self.assertIn('pv_power_raw', st['inverter']['data'])
        self.assertEqual(st['holding']['decoded']['battery_type'], 'lithium')
        st = self.client.wait_for(lambda m: m.get('type') == 'state' and m['emulator']['polls'] > 0)
        self.assertTrue(st['emulator']['polling'] and st['emulator']['charge_ok'])
        self.assertEqual(len(st['emulator']['regs']), 16)
        self.assertEqual(set(st['workers']), {'jk', 'emulator', 'inverter', 'pi'})

    def test_quick_charge_writes_and_refusals(self):
        r = self.write([[233, 1], [234, 30]], 'qc')
        self.assertTrue(r['ok'], r)
        self.assertEqual([x['readback'] for x in r['results']], [1, 30])
        r = self.write([[234, 500]], 'long')
        self.assertFalse(r['ok'])
        self.assertIn('5..60', r['results'][0]['error'])
        r = self.write([[21, NORMAL | 0x80]], 'acchg')
        self.assertFalse(r['ok'])
        r = self.write([[66, 30]], 'other')
        self.assertIn('not writable', r['results'][0]['error'])
        r = self.write([[233, 0]], 'stop')
        self.assertTrue(r['ok'])
        r = self.write([[234, 30]], 'inactive')        # firmware rejects the countdown while quick charge is off
        self.assertFalse(r['ok'])
        self.assertIn('illegal data value', r['results'][0]['error'])

    def test_ct_calibration_runs(self):
        st = self.client.wait_for(lambda m: m.get('type') == 'state' and (m.get('ct') or {}).get('writes', 0) >= 1)
        ct = st['ct']
        self.assertTrue(ct['enabled'])
        self.assertIsNotNone(ct['last_write_ts'])
        self.assertLessEqual(ct['offset_w'], 0, 'never biased toward export')
        self.assertTrue(ct['pending'] or abs(ct['offset_w'] - ct['target_w']) < 20)
        self.assertEqual((ct['slope'], ct['max_w'], ct['firmware_min_w']), (-0.045, 0.0, -250.0))
        st = self.client.wait_for(lambda m: m.get('type') == 'state' and 'ct_power_offset_target' in (m['inverter']['data'] or {}))
        self.assertIsNotNone(st['inverter']['data']['ct_power_offset'])

    def test_standby_watchdog_restores_normal(self):
        r = self.write([[21, STANDBY]], 'standby')
        self.assertTrue(r['ok'], r)
        ev = self.client.wait_for(lambda m: m.get('type') == 'event' and 'standby watchdog' in m['msg'], timeout=20)
        self.assertIn('ok', ev['msg'])
        st = self.client.wait_for(lambda m: m.get('type') == 'state' and not m['holding']['decoded']['standby'])
        self.assertFalse(st['holding']['decoded']['standby'])


if __name__ == '__main__':
    unittest.main()
