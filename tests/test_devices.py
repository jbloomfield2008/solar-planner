"""Device protocol tests: Modbus framing, FlexBoss decoding, JK frame parsing, Lux battery emulator."""
import struct
import unittest

from solar01.config import EmulatorConfig
from solar01.devices import bmsemu, flexboss, jkbms, modbus


class FakeSerial:
    """Scripted serial port: replies[request bytes] -> response bytes."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.written = []
        self.buf = b''
        self.closed = False

    def reset_input_buffer(self):
        self.buf = b''

    def write(self, data):
        self.written.append(bytes(data))
        self.buf += self.replies.get(bytes(data), b'')

    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def close(self):
        self.closed = True


def master_with(replies):
    ser = FakeSerial(replies)
    return modbus.RtuMaster('fake', 19200, slave=1, gap_s=0, serial_factory=lambda *a: ser), ser


class ModbusTest(unittest.TestCase):
    def test_crc_vectors(self):
        self.assertEqual(modbus.with_crc(bytes.fromhex('0103002d005b')), bytes.fromhex('0103002d005b9438'))
        self.assertEqual(modbus.with_crc(bytes.fromhex('010300001000')), bytes.fromhex('010300001000480a'))
        self.assertTrue(modbus.crc_ok(bytes.fromhex('010300001000480a')))
        self.assertFalse(modbus.crc_ok(bytes.fromhex('010300001000480b')))

    def test_read_input(self):
        req = modbus.with_crc(bytes.fromhex('010400000002'))
        resp = modbus.with_crc(bytes.fromhex('01040400140033'))
        m, _ = master_with({req: resp})
        self.assertEqual(m.read_input(0, 2), (0x14, 0x33))

    def test_exception_reply(self):
        req = modbus.with_crc(struct.pack('>BBHH', 1, 6, 234, 30))
        m, _ = master_with({req: modbus.with_crc(bytes((1, 0x86, 3)))})
        with self.assertRaises(modbus.ModbusException) as cm:
            m.write_holding(234, 30)
        self.assertEqual(cm.exception.code, 3)

    def test_no_reply_and_bad_crc(self):
        m, _ = master_with({})
        with self.assertRaises(modbus.LinkError):
            m.read_holding(21, 1)
        req = modbus.with_crc(bytes.fromhex('010300150001'))
        bad = bytearray(modbus.with_crc(bytes.fromhex('0103027254')))
        bad[-1] ^= 0xFF
        m, _ = master_with({req: bytes(bad)})
        with self.assertRaises(modbus.LinkError):
            m.read_holding(21, 1)

    def test_write_echo(self):
        req = modbus.with_crc(struct.pack('>BBHH', 1, 6, 21, 29268))
        m, ser = master_with({req: req})
        m.write_holding(21, 29268)
        self.assertEqual(ser.written, [req])


class FlexBossTest(unittest.TestCase):
    def test_decode_input(self):
        r = {i: 0 for i in range(245)}
        r.update({0: 0x0C, 1: 3456, 4: 535, 5: 0x6454, 7: 1000, 8: 200, 9: 0, 10: 1500, 11: 0, 12: 2455, 15: 6001,
                  26: 0, 27: 40, 28: 50, 29: 10, 30: 0, 33: 72, 34: 31, 140: 1230, 141: 1225, 170: 667,
                  171: 88, 172: 0x2345, 173: 0x0001, 210: 1500})
        d = flexboss.decode_input(r)
        self.assertEqual(d['mode'], 'PV charge + on-grid')
        self.assertEqual(d['soc'], 0x54)
        self.assertEqual(d['pv_power'], 1200)
        self.assertEqual(d['battery_power'], 1500)
        self.assertAlmostEqual(d['battery_voltage'], 53.5)
        self.assertAlmostEqual(d['grid_frequency'], 60.01)
        self.assertAlmostEqual(d['pv_energy_today'], 6.0)
        self.assertAlmostEqual(d['load_energy_total'], (0x2345 + (1 << 16)) / 10)
        self.assertEqual(d['quick_charge_remaining'], 1500)
        self.assertEqual(flexboss.decode_input({**r, 0: 0x77})['mode'], 'Unknown (0x77)')

    def test_holding_snapshot_signed_ct_offset(self):
        replies = {}

        def add(base, values):
            req = modbus.with_crc(struct.pack('>BBHH', 1, 3, base, len(values)))
            body = b''.join(struct.pack('>H', v & 0xFFFF) for v in values)
            replies[req] = modbus.with_crc(bytes((1, 3, len(body))) + body)
        add(0, [0x8200, 265])
        add(21, [29268])
        add(60, [56])
        add(66, [30, 70])
        add(101, [40, 100, 0, 0, 15])
        add(119, [-200])
        add(233, [1, 25])
        m, _ = master_with(replies)
        snap = flexboss.FlexBoss(m).read_holding_snapshot()
        self.assertEqual(snap[21], 29268)
        self.assertEqual((snap[0], snap[66], snap[67]), (0x8200, 30, 70))
        self.assertEqual(snap[119], -200)
        self.assertEqual((snap[101], snap[105], snap[233], snap[234]), (40, 15, 1, 25))


def jk_frame(cells=(3342,) * 16, voltage=53.5, current=26.8, soc=51, temps=(29, 29, 29), cycles=260,
             status=0x0003, capacity=280, pad=True):
    def u16(v):
        return struct.pack('>H', v & 0xFFFF)
    data = bytearray()
    cell_block = b''.join(bytes((i + 1,)) + u16(v) for i, v in enumerate(cells))
    data += bytes((0x79, len(cell_block))) + cell_block
    data += b'\x80' + u16(temps[0]) + b'\x81' + u16(temps[1]) + b'\x82' + u16(temps[2])
    data += b'\x83' + u16(round(voltage * 100))
    raw_cur = round(abs(current) * 100) | (0x8000 if current > 0 else 0)
    data += b'\x84' + u16(raw_cur) + b'\x85' + bytes((soc,)) + b'\x86\x02' + b'\x87' + u16(cycles)
    data += b'\x89' + struct.pack('>I', 1000) + b'\x8a' + u16(16) + b'\x8b' + u16(0) + b'\x8c' + u16(status)
    if pad:                                   # real frames carry ~270 bytes of settings and strings
        for fid in range(0x8e, 0xa9):
            if fid != 0x9d:
                data += bytes((fid,)) + u16(0)
        data += b'\xb7' + b'11.XA_S11.48___' + b'\xba' + b'JK_B2A16S20P___________0'
    data += b'\xaa' + struct.pack('>I', capacity) + b'\x68' + b'\x00' * 8
    header = b'NW' + u16(len(data) + 9) + b'\x00' * 4 + b'\x06\x00\x01'
    return bytes(header + data)


class JkTest(unittest.TestCase):
    def test_parse(self):
        cells = [3342] * 15 + [3328]
        d = jkbms.parse(jk_frame(cells=cells), 16)
        self.assertIsNotNone(d)
        self.assertEqual((d['soc'], d['capacity'], d['cycle_count']), (51, 280, 260))
        self.assertAlmostEqual(d['voltage'], 53.5)
        self.assertAlmostEqual(d['current'], 26.8)
        self.assertEqual((d['cell_voltage_max'], d['cell_voltage_min'], d['cell_voltage_delta']), (3342, 3328, 14))
        self.assertEqual(d['cells']['cell_16'], 3328)
        self.assertTrue(d['charge_mos_on'] and d['discharge_mos_on'] and not d['balancing'])

    def test_discharge_sign_and_negative_temp(self):
        d = jkbms.parse(jk_frame(current=-12.5, temps=(30, 103, 25)), 16)
        self.assertAlmostEqual(d['current'], -12.5)
        self.assertEqual(d['temp_1'], -3)

    def test_rejects_garbage(self):
        self.assertIsNone(jkbms.parse(b'NW' + b'\x00' * 300))
        self.assertIsNone(jkbms.parse(jk_frame(pad=False)[:150]))
        self.assertIsNone(jkbms.parse(jk_frame(cells=(3342,) * 15), 16), 'wrong cell count')
        self.assertIsNone(jkbms.parse(jk_frame(cells=(3342,) * 15 + (65535,)), 16), 'implausible cell')
        self.assertIsNone(jkbms.parse(jk_frame(voltage=5.0), 16), 'implausible pack voltage')

    def test_poll_uses_request(self):
        ser = FakeSerial({jkbms.REQUEST: jk_frame()})
        bms = jkbms.JkBms('fake', settle_s=0, serial_factory=lambda *a: ser)
        self.assertEqual(bms.poll()['soc'], 51)
        self.assertEqual(ser.written, [jkbms.REQUEST])


JK = {'capacity': 280, 'cell_voltage_max': 3436, 'cell_voltage_min': 3410, 'charge_mos_on': True, 'current': -0.4,
      'cycle_count': 254, 'discharge_mos_on': True, 'soc': 100, 'temp_1': 33, 'temp_2': 32, 'temp_mos': 35,
      'voltage': 54.85}


class EmulatorTest(unittest.TestCase):
    """Ported from the live-verified bms_emu.py tests."""

    def setUp(self):
        self.cfg = EmulatorConfig()
        self.lim = bmsemu.ChargeLimiter(self.cfg)

    def build(self, jk, age=1.0):
        return bmsemu.build_registers(jk, age, self.cfg, self.lim)

    def test_registers(self):
        regs, info = self.build(JK)
        self.assertEqual(regs, [554, 1000, 1500, (100 << 8) | 100, 280, 548, (-40) & 0xFFFF, 3, 0, 0, 3436, 3410,
                                330, 320, 0, 254])
        self.assertEqual(info['reason'], 'normal')

    def test_poll_reply_little_endian(self):
        regs, _ = self.build(JK)
        slave = bmsemu.LuxSlave(self.cfg, lambda: self.build(JK))
        out = slave.handle(bytes.fromhex('010300001000480a'))
        self.assertEqual(len(out), 3 + 32 + 2)
        self.assertEqual(out[:3], bytes((1, 3, 32)))
        self.assertTrue(modbus.crc_ok(out))
        self.assertEqual(list(struct.unpack('<16H', out[3:-2])), regs)
        self.assertEqual(slave.polls, 1)
        self.assertIsNotNone(slave.poll_age())

    def test_other_address_ignored(self):
        slave = bmsemu.LuxSlave(self.cfg, lambda: self.build(JK))
        self.assertIsNone(slave.handle(modbus.with_crc(bytes.fromhex('100300001000'))))
        self.assertEqual(slave.other_addrs, {16: 1})

    def test_bad_crc_counted(self):
        slave = bmsemu.LuxSlave(self.cfg, lambda: self.build(JK))
        self.assertIsNone(slave.handle(bytes.fromhex('010300001000480b')))
        self.assertEqual(slave.crc_errors, 1)

    def test_taper_and_latch(self):
        seen = []
        for cm in (3440, 3500, 3550, 3500, 3399):
            regs, info = self.build(dict(JK, cell_voltage_max=cm))
            seen.append((info['max_chg_a'], info['chg_volt'], regs[7]))
        self.assertEqual(seen[0], (100.0, 55.4, 3))
        self.assertEqual(seen[1][0], round(5 + 95 * 0.5))
        self.assertEqual(seen[2], (0.0, 54.0, 2))
        self.assertEqual(seen[3], (0.0, 54.0, 2), 'latched until below reset')
        self.assertEqual(seen[4], (100.0, 55.4, 3))

    def test_protections(self):
        regs, _ = self.build(dict(JK, discharge_mos_on=False))
        self.assertEqual((regs[7], regs[2]), (1, 0))
        regs, _ = self.build(dict(JK, temp_1=0))
        self.assertEqual((regs[7], regs[1]), (2, 0))
        regs, _ = self.build(dict(JK, cell_voltage_min=2800))
        self.assertEqual(regs[7] & 2, 0)

    def test_stale_then_dead(self):
        regs, info = self.build(JK, age=60)
        self.assertEqual((regs[7], regs[1]), (2, 0))
        self.assertIn('stale', info['reason'])
        regs, info = self.build(JK, age=400)
        self.assertIsNone(regs)
        regs, info = self.build(None, age=0)
        self.assertIsNone(regs)
        slave = bmsemu.LuxSlave(self.cfg, lambda: self.build(JK, age=400))
        self.assertIsNone(slave.handle(bytes.fromhex('010300001000480a')))
        self.assertEqual(slave.unanswered, 1)


if __name__ == '__main__':
    unittest.main()
