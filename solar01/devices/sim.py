"""Simulated FlexBoss21 + JK BMS (core.simulate = true) for development without hardware.

Models the firmware quirks the core and planner rely on: H234 is rejected while quick
charge is off, enabling quick charge starts a 60-minute countdown, standby idles PV and
battery while the grid feeds the loads.
"""
from __future__ import annotations

import datetime as dt
import math
import random
import threading
import time

from . import flexboss
from .modbus import ModbusException


class SimPlant:
    def __init__(self, soc: float = 55.0, cap_ah: int = 280):
        self.lock = threading.Lock()
        self.soc = soc
        self.cap_ah = cap_ah
        self.holding = {0: 0x8200, 1: 265, 21: 29268, 60: 56, 66: 30, 67: 70, 101: 140, 102: 140, 103: 0, 104: 0, 105: 15,
                        119: 200, 233: 0, 234: 0}
        self.qc_deadline: float | None = None
        self.last = time.monotonic()
        self.pv = self.load = self.batt = self.grid = 0.0
        self.today = {'pv': 0.0, 'load': 0.0, 'chg': 0.0, 'dis': 0.0, 'imp': 0.0}
        self.load_total_kwh = 18234.5

    def step(self) -> None:
        with self.lock:
            now = time.monotonic()
            dh = min(now - self.last, 60.0) / 3600
            self.last = now
            t = dt.datetime.now()
            hour = t.hour + t.minute / 60
            pv = max(0.0, 2400 * math.exp(-((hour - 12.8) / 3.0) ** 2) - 60) * random.uniform(0.92, 1.0) \
                if 6 < hour < 20 else 0.0
            load = 380 + 700 * math.exp(-((hour - 19.5) / 1.3) ** 2) + random.uniform(-40, 140)
            if self.qc_deadline is not None and now >= self.qc_deadline:
                self.holding[233] &= ~flexboss.BIT_QC
                self.holding[234] = 0
                self.qc_deadline = None
            standby = not self.holding[21] & flexboss.BIT_NORMAL
            qc = bool(self.holding[233] & flexboss.BIT_QC)
            if standby:
                pv, batt = 0.0, 0.0
            elif qc:
                batt = 0.0 if self.soc >= 100 else min(2100.0 + max(pv - load, 0.0), 2600.0)
            else:
                batt = max(-7000.0, min(pv - load, 5000.0))
                if (self.soc >= 100 and batt > 0) or (self.soc <= 12 and batt < 0):
                    batt = 0.0
            pv = min(pv, load + max(batt, 0.0)) if not qc else pv      # zero sell-back curtails PV
            grid = max(0.0, load + batt - pv)
            self.soc = max(0.0, min(100.0, self.soc + batt * dh / (self.cap_ah * 51.2) * 100))
            self.pv, self.load, self.batt, self.grid = pv, load, batt, grid
            for k, v in (('pv', pv), ('load', load), ('chg', max(batt, 0)), ('dis', max(-batt, 0)), ('imp', grid)):
                self.today[k] += v * dh / 1000
            self.load_total_kwh += load * dh / 1000

    def mode(self) -> int:
        if not self.holding[21] & flexboss.BIT_NORMAL:
            return 0x00
        if self.holding[233] & flexboss.BIT_QC:
            return 0x28
        if self.batt > 0:
            return 0x0C
        if self.batt < 0:
            return 0x14 if self.pv > 0 else 0x10
        return 0x04


class SimInverter:
    def __init__(self, plant: SimPlant):
        self.plant = plant

    def close(self) -> None:
        pass

    def read_inputs(self) -> dict:
        p = self.plant
        p.step()
        time.sleep(0.02)
        with p.lock:
            r = {i: 0 for i in range(245)}
            vbat = 51.0 + p.soc * 0.035
            r.update({0: p.mode(), 1: 3800 if p.pv else 0, 4: round(vbat * 10), 5: (100 << 8) | round(p.soc),
                      7: round(p.pv), 10: round(max(p.batt, 0)), 11: round(max(-p.batt, 0)), 12: 2462, 15: 6000,
                      16: round(abs(p.pv - max(p.batt, 0))), 18: 250, 27: round(p.grid),
                      28: round(p.today['pv'] * 10), 33: round(p.today['chg'] * 10),
                      34: round(p.today['dis'] * 10), 37: round(p.today['imp'] * 10), 64: 41, 65: 38, 66: 37,
                      139: round(p.grid / 2.4), 140: 1231, 141: 1229, 170: round(p.load),
                      171: round(p.today['load'] * 10)})
            total = round(p.load_total_kwh * 10)
            r[172], r[173] = total & 0xFFFF, total >> 16
            if p.qc_deadline is not None:
                r[210] = max(0, round(p.qc_deadline - time.monotonic())) & 0xFFFF
        return flexboss.decode_input(r)

    def read_holding_snapshot(self) -> dict[int, int]:
        with self.plant.lock:
            h = dict(self.plant.holding)
        return {k: flexboss.signed16(v) if k in flexboss.SIGNED_HOLDING else v for k, v in h.items()}

    def read_holding(self, reg: int) -> int:
        with self.plant.lock:
            v = self.plant.holding.get(reg, 0)
        return flexboss.signed16(v) if reg in flexboss.SIGNED_HOLDING else v

    def write_holding(self, reg: int, value: int) -> None:
        p = self.plant
        with p.lock:
            if reg == flexboss.H_QC_MINUTES:
                if not p.holding[233] & flexboss.BIT_QC or not 5 <= value <= 1440:
                    raise ModbusException(3)
                p.holding[234] = value
                p.qc_deadline = time.monotonic() + value * 60
            elif reg == flexboss.H_QC:
                if value & flexboss.BIT_QC and not p.holding[233] & flexboss.BIT_QC:
                    p.holding[234] = 60
                    p.qc_deadline = time.monotonic() + 3600
                elif not value & flexboss.BIT_QC:
                    p.holding[234] = 0
                    p.qc_deadline = None
                p.holding[233] = value
            else:
                p.holding[reg] = value & 0xFFFF


class SimJk:
    def __init__(self, plant: SimPlant):
        self.plant = plant

    def close(self) -> None:
        pass

    def poll(self) -> dict:
        p = self.plant
        time.sleep(0.05)
        with p.lock:
            soc, batt = p.soc, p.batt
        v = 51.0 + soc * 0.035
        base = 3190 + soc * 1.5
        cells = {f'cell_{i:02d}': round(base + random.uniform(-6, 6)) for i in range(1, 17)}
        vals = list(cells.values())
        cur = round(batt / v, 2)
        return {'voltage': round(v, 2), 'current': cur, 'power': round(v * cur, 1), 'soc': round(soc),
                'capacity': p.cap_ah, 'remaining_capacity': round(p.cap_ah * soc / 100, 1), 'cycle_count': 261,
                'temp_mos': 31, 'temp_1': 27, 'temp_2': 27, 'cell_voltage_min': min(vals),
                'cell_voltage_max': max(vals), 'cell_voltage_delta': max(vals) - min(vals), 'cells': cells,
                'charge_mos_on': True, 'discharge_mos_on': True, 'balancing': False, 'warning_bits': 0}
