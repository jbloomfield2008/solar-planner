"""EG4 FlexBoss21 on its meter port: Modbus RTU 19200 8N1, slave 1, Luxpower register map.

Verified facts (2026-08/09, firmware on serial 4563E00048):
* Input registers are read in 40-register blocks at 0, 40, 125, 165, 205.
* I172/I173 = total load energy, U32 (low, high) x 0.1 kWh.  There are no per-leg
  grid power registers on this inverter.
* I210 = seconds left on quick charge (16-bit).
* H21 bit 7 = AC Charge function.  NOT used: while set, loads stay on grid even when
  the battery is not charging.
* H21 bit 9 = 1 normal / 0 standby.  In standby the grid bypass feeds the loads and PV
  and battery sit idle; waking up takes ~5-9 minutes.
* H233 bit 0 = quick charge enable.  H234 = countdown minutes, writable only while the
  quick charge is active (5..1440), otherwise "illegal data value".  Enabling starts
  with the firmware default of 60 minutes.  Clearing bit 0 stops and zeroes H234.
* H119 = CT power offset, signed, LSB 0.1 W, clamped by the firmware to +5000/-2500.
* H60 = active power percent (output cap, set to 56 by the user), H101/H102 = charge /
  discharge current limit (A), H105 = end-of-discharge SOC.
"""
from __future__ import annotations

from .modbus import RtuMaster

MODE_NAMES = {
    0x00: 'Standby', 0x01: 'Fault', 0x02: 'Programming', 0x04: 'PV on-grid', 0x08: 'PV charge',
    0x0C: 'PV charge + on-grid', 0x10: 'Battery on-grid', 0x14: 'PV + battery on-grid', 0x20: 'AC charge',
    0x28: 'PV + AC charge', 0x40: 'Battery off-grid', 0x80: 'PV off-grid', 0x88: 'PV charge + off-grid',
    0xC0: 'PV + battery off-grid',
}
MODE_AC_CHARGE = 0x20

INPUT_BLOCKS = ((0, 40), (40, 40), (125, 40), (165, 40), (205, 40))

H_FUNC = 21
BIT_AC_CHARGE = 0x0080
BIT_NORMAL = 0x0200
H_ACTIVE_POWER_PCT = 60
H_AC_CHG_POWER = 66          # 0.1 kW (30 = 3.0 kW); read-only here, bounds the grid charge rate
H_AC_CHG_SOC_LIMIT = 67
H_CHG_CURRENT_A = 101
H_DISCHG_CURRENT_A = 102
H_EOD_SOC = 105
H_CT_OFFSET = 119
H_QC = 233
BIT_QC = 0x0001
H_QC_MINUTES = 234
HOLDING_BLOCKS = ((0, 2), (21, 1), (60, 1), (66, 2), (101, 5), (119, 1), (233, 2))
SIGNED_HOLDING = {H_CT_OFFSET}
CT_REG_PER_W = 10
CT_REG_MIN, CT_REG_MAX = -2500, 5000


def signed16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def decode_input(r: dict[int, int]) -> dict:
    """Input registers -> engineering values (same keys the MQTT bridge has always used)."""
    return {
        'state': r[0],
        'mode': MODE_NAMES.get(r[0], f'Unknown (0x{r[0]:02X})'),
        'pv1_voltage': r[1] / 10,
        'pv2_voltage': r[2] / 10,
        'battery_voltage': r[4] / 10,
        'soc': r[5] & 0xFF,
        'pv_power': r[7] + r[8] + r[9],
        'battery_charge_power': r[10],
        'battery_discharge_power': r[11],
        'battery_power': r[10] - r[11],
        'grid_voltage': r[12] / 10,
        'grid_frequency': r[15] / 100,
        'inverter_power': r[16],
        'inverter_current': r[18] / 100,
        'grid_export_power': r[26],
        'grid_import_power': r[27],
        'pv_energy_today': (r[28] + r[29] + r[30]) / 10,
        'charge_energy_today': r[33] / 10,
        'discharge_energy_today': r[34] / 10,
        'export_energy_today': r[36] / 10,
        'import_energy_today': r[37] / 10,
        'temp_internal': r[64],
        'temp_radiator1': r[65],
        'temp_radiator2': r[66],
        'ct_current': r[139] / 100,
        'grid_voltage_l1': r[140] / 10,
        'grid_voltage_l2': r[141] / 10,
        'load_power': r[170],
        'load_energy_today': r[171] / 10,
        'load_energy_total': (r[172] + (r[173] << 16)) / 10,
        'quick_charge_remaining': r[210],
    }


class FlexBoss:
    def __init__(self, master: RtuMaster):
        self.master = master

    def read_inputs(self) -> dict:
        r: dict[int, int] = {}
        for base, count in INPUT_BLOCKS:
            for i, v in enumerate(self.master.read_input(base, count)):
                r[base + i] = v
        return decode_input(r)

    def read_holding_snapshot(self) -> dict[int, int]:
        snap: dict[int, int] = {}
        for base, count in HOLDING_BLOCKS:
            for i, v in enumerate(self.master.read_holding(base, count)):
                reg = base + i
                snap[reg] = signed16(v) if reg in SIGNED_HOLDING else v
        return snap

    def read_holding(self, reg: int) -> int:
        v = self.master.read_holding(reg, 1)[0]
        return signed16(v) if reg in SIGNED_HOLDING else v

    def write_holding(self, reg: int, value: int) -> None:
        self.master.write_holding(reg, value)
