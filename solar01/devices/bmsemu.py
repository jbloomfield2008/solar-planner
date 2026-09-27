"""Luxpower-protocol battery emulator for the FlexBoss21 battery port.

The inverter (Battery Type = Lithium, Lithium Brand = 0:EG4) is the Modbus master
at 9600 8N1 and polls every 500 ms with ``01 03 00 00 10 00 48 0A`` (addr 1,
FC03, start 0, "count" 0x1000).  It expects a 16-register reply with
LITTLE-ENDIAN words - the protocol EG4-LL batteries speak when set to
"P-03 LUX".  Layout, reverse-engineered 2026-09-01 by feeding index patterns
and reading back the inverter's own BMS input registers:

    reg  meaning                                   unit     inverter copy
     0   charge voltage reference                  0.1 V    I83
     1   max charge current                        0.1 A    I81
     2   max discharge current                     0.1 A    I82
     3   SOH << 8 | SOC                            %        I5
     4   capacity                                  Ah       I97
     5   pack voltage                              0.1 V    I4
     6   current, signed                           0.01 A   I98
     7   bit0 charge allowed, bit1 discharge allowed        I90 / I95
     8   fault code                                         I99
     9   warning code                                       I100
    10   max cell voltage                          mV       I101
    11   min cell voltage                          mV       I102
    12   max cell temperature                      0.1 C    I103
    13   min cell temperature                      0.1 C    I104
    14   unknown (not surfaced)
    15   cycle count                                        I106

A status word of 0 shows "Forbidden" on the inverter LCD and the inverter ignores
the battery.  Big-endian or EG4-LL layout replies are accepted but mis-decoded.
"""
from __future__ import annotations

import logging
import struct
import time

from .modbus import crc16, crc_ok, with_crc

log = logging.getLogger('solar01.emulator')

LUX_REGS = 16
ST_CHARGE_OK, ST_DISCHARGE_OK = 0x0001, 0x0002


class ChargeLimiter:
    """Charge current taper with a latch: above taper_end the charge is forbidden and the
    voltage request drops to float until the highest cell falls below taper_reset."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.taper_latched = False

    def max_charge_a(self, jk: dict) -> tuple[float, str]:
        c = self.cfg
        cmax = jk.get('cell_voltage_max') or 0
        temps = [t for t in (jk.get('temp_1'), jk.get('temp_2')) if t is not None]
        tmin = min(temps) if temps else 25
        tmax = max(temps) if temps else 25
        if not jk.get('charge_mos_on', True):
            return 0.0, 'JK charge MOS off'
        if tmin < c.chg_min_temp_c:
            return 0.0, f'cold {tmin} C'
        if tmax > c.chg_max_temp_c:
            return 0.0, f'hot {tmax} C'
        if cmax >= c.taper_end_mv:
            self.taper_latched = True
        elif cmax < c.taper_reset_mv:
            self.taper_latched = False
        if self.taper_latched:
            return 0.0, f'full (cell {cmax} mV, latched)'
        if cmax > c.taper_start_mv:
            frac = (c.taper_end_mv - cmax) / (c.taper_end_mv - c.taper_start_mv)
            return round(c.taper_min_a + (c.chg_max_a - c.taper_min_a) * frac), f'taper (cell {cmax} mV)'
        return c.chg_max_a, 'normal'


def build_registers(jk: dict | None, age_s: float, cfg, limiter: ChargeLimiter):
    """Return (16 registers, info) or (None, info) when the emulator must go silent."""
    if jk is None or age_s > cfg.dead_s:
        return None, {'age': age_s, 'reason': 'dead' if jk else 'no data'}
    stale = age_s > cfg.stale_s
    v = float(jk.get('voltage', 0))
    i = float(jk.get('current', 0)) * cfg.current_sign
    cmax = int(jk.get('cell_voltage_max') or 0)
    cmin = int(jk.get('cell_voltage_min') or 0)
    temps = [float(t) for t in (jk.get('temp_1'), jk.get('temp_2')) if t is not None] or [25.0]
    soc = max(0, min(100, int(jk.get('soc', 0))))
    cap = float(jk.get('capacity', cfg.design_ah)) or cfg.design_ah
    cycles = int(jk.get('cycle_count', 0))

    if stale:
        chg_a, reason = 0.0, f'stale {age_s:.0f}s'
    else:
        chg_a, reason = limiter.max_charge_a(jk)
    chg_volt = cfg.float_volt if limiter.taper_latched else cfg.chg_volt
    dischg_ok = bool(jk.get('discharge_mos_on', True)) and not (cmin and cmin <= cfg.cell_uv_mv)
    dischg_a = cfg.dischg_max_a if dischg_ok else 0.0
    status = (ST_CHARGE_OK if chg_a > 0 else 0) | (ST_DISCHARGE_OK if dischg_ok else 0)
    regs = [
        int(round(chg_volt * 10)),
        int(round(chg_a * 10)),
        int(round(dischg_a * 10)),
        ((cfg.soh & 0xFF) << 8) | soc,
        int(round(cap)),
        int(round(v * 10)),
        int(round(i * 100)) & 0xFFFF,
        status,
        0,
        0,
        cmax,
        cmin,
        int(round(max(temps) * 10)) & 0xFFFF,
        int(round(min(temps) * 10)) & 0xFFFF,
        0,
        cycles & 0xFFFF,
    ]
    info = {'age': age_s, 'stale': stale, 'max_chg_a': chg_a, 'max_dischg_a': dischg_a, 'chg_volt': chg_volt,
            'reason': reason, 'status': status, 'cell_max': cmax,
            'charge_ok': bool(status & ST_CHARGE_OK), 'discharge_ok': bool(status & ST_DISCHARGE_OK)}
    return regs, info


class LuxSlave:
    """Answers the inverter's polls.  provider() -> (registers | None, info)."""

    def __init__(self, cfg, provider):
        self.cfg = cfg
        self.provider = provider
        self.addrs = set(cfg.addrs)
        self.polls = 0
        self.last_poll_mono = 0.0
        self.other_addrs: dict[int, int] = {}
        self.writes = 0
        self.crc_errors = 0
        self.unanswered = 0
        self.last_regs = None

    def poll_age(self) -> float | None:
        return None if not self.last_poll_mono else time.monotonic() - self.last_poll_mono

    def handle(self, frame: bytes) -> bytes | None:
        if len(frame) < 4:
            return None
        if not crc_ok(frame):
            self.crc_errors += 1
            log.warning('bad CRC: %s', frame.hex(' '))
            return None
        addr, func = frame[0], frame[1]
        if addr not in self.addrs:
            self.other_addrs[addr] = self.other_addrs.get(addr, 0) + 1
            return None
        self.polls += 1
        self.last_poll_mono = time.monotonic()
        if func == 0x03 and len(frame) == 8:
            start, count = struct.unpack('>HH', frame[2:6])
            regs, info = self.provider()
            if regs is None:
                self.unanswered += 1
                if self.unanswered % 20 == 1:
                    log.warning('JK data %s -> not answering polls', info.get('reason'))
                return None
            self.last_regs = regs
            if count > 125:                      # the inverter's 0x1000 "count" -> 16-register reply
                count = len(regs)
            words = [regs[a] if a < len(regs) else 0 for a in range(start, start + count)]
            body = b''.join(struct.pack('<H', w) for w in words)
            resp = bytes((addr, 0x03, len(body))) + body
        elif func in (0x06, 0x10) and len(frame) >= 8:
            self.writes += 1
            log.info('write request (ack only): %s', frame.hex(' '))
            resp = frame[:6]
        else:
            resp = bytes((addr, func | 0x80, 0x01))
        out = with_crc(resp)
        if self.cfg.log_frames:
            log.info('req %s -> %s', frame.hex(' '), out.hex(' '))
        return out

    @staticmethod
    def read_frame(ser) -> bytes | None:
        """One RTU frame: bytes until an inter-frame gap (serial timeout ~20 ms at 9600)."""
        buf = ser.read(1)
        if not buf:
            return None
        while True:
            more = ser.read(256)
            if not more:
                return buf
            buf += more


__all__ = ['ChargeLimiter', 'LuxSlave', 'build_registers', 'crc16', 'with_crc']
