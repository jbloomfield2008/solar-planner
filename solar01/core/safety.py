"""Rules the core enforces no matter what the hub (or anything else) asks for.

Only three holding registers can ever be written on request:

* H21  - only bit 9 (standby) may change.  The AC Charge bit and everything else is
  preserved exactly, so a planner bug cannot enable AC charge mode.
* H233 - only bit 0 (quick charge) may change.
* H234 - quick-charge countdown, 5..qc_max_arm_min minutes, so a stuck hub can never
  leave a long grid charge armed; the firmware stops it when the countdown ends.

H119 (CT offset) is written only by the core's own calibration loop.
"""
from __future__ import annotations

from ..devices.flexboss import (BIT_AC_CHARGE, BIT_NORMAL, BIT_QC, CT_REG_MAX, CT_REG_MIN, CT_REG_PER_W,
                                H_ACTIVE_POWER_PCT, H_CHG_CURRENT_A, H_CT_OFFSET, H_DISCHG_CURRENT_A, H_EOD_SOC,
                                H_FUNC, H_QC, H_QC_MINUTES)

WRITABLE = (H_FUNC, H_QC, H_QC_MINUTES)
NEEDS_CURRENT = (H_FUNC, H_QC)


def check_write(reg: int, value: int, current: int | None, qc_max_arm_min: int) -> str | None:
    """None if the write is allowed, otherwise the reason it is refused."""
    if not isinstance(value, int) or not 0 <= value <= 0xFFFF:
        return 'value must be 0..65535'
    if reg == H_FUNC:
        if current is None:
            return 'H21: current value unknown'
        if (current ^ value) & ~BIT_NORMAL & 0xFFFF:
            return 'H21: only bit 9 (standby) may be changed'
        return None
    if reg == H_QC:
        if current is None:
            return 'H233: current value unknown'
        if (current ^ value) & ~BIT_QC & 0xFFFF:
            return 'H233: only bit 0 (quick charge) may be changed'
        return None
    if reg == H_QC_MINUTES:
        if not 5 <= value <= qc_max_arm_min:
            return f'H234: countdown must be 5..{qc_max_arm_min} minutes'
        return None
    return f'H{reg} is not writable'


def ct_offset_target(load_w: float, cfg) -> int:
    """CT offset register value for the current load (register units, 0.1 W)."""
    w = cfg.ct_cal_slope * load_w + cfg.ct_cal_intercept
    w = max(cfg.ct_cal_min_w, min(cfg.ct_cal_max_w, w))
    return max(CT_REG_MIN, min(CT_REG_MAX, int(round(w * CT_REG_PER_W))))


def ct_should_write(target: int, current: int | None, since_last_write_s: float | None, cfg) -> bool:
    if cfg.ct_cal_slope == 0:
        return False
    if since_last_write_s is not None and since_last_write_s < cfg.ct_cal_min_write_s:
        return False
    return current is None or abs(target - current) >= cfg.ct_cal_hyst_w * CT_REG_PER_W


def standby_watchdog_due(h21: int | None, since_assert_s: float, cfg) -> bool:
    return h21 is not None and not h21 & BIT_NORMAL and since_assert_s > cfg.standby_watchdog_s


BATTERY_TYPES = {0: 'no battery', 1: 'lead-acid', 2: 'lithium'}


def decode_holding(h: dict[int, int]) -> dict:
    out: dict = {}
    if 0 in h:
        # H0 high byte: bits 0-1 battery type, bits 2-6 lithium brand (inferred from 0x8100 lead-acid
        # before 2026-09-01, 0x8200 Lithium/0:EG4 after; input register I80 mirrors the low 7 bits).
        hb = (h[0] >> 8) & 0xFF
        out['h0'] = f'0x{h[0]:04x}'
        out['battery_type'] = BATTERY_TYPES.get(hb & 0x3, f'unknown ({hb & 0x3})')
        out['lithium_brand'] = (hb >> 2) & 0x1F
        out['bms_closed_loop'] = (hb & 0x3) == 2 and (hb >> 2) & 0x1F == 0
    if H_FUNC in h:
        out['standby'] = not h[H_FUNC] & BIT_NORMAL
        out['ac_charge_function'] = bool(h[H_FUNC] & BIT_AC_CHARGE)
    if H_QC in h:
        out['quick_charge'] = bool(h[H_QC] & BIT_QC)
    if H_QC_MINUTES in h:
        out['quick_charge_minutes'] = h[H_QC_MINUTES]
    if H_CHG_CURRENT_A in h:
        out['charge_current_limit_a'] = h[H_CHG_CURRENT_A]
    if H_DISCHG_CURRENT_A in h:
        out['discharge_current_limit_a'] = h[H_DISCHG_CURRENT_A]
    if H_EOD_SOC in h:
        out['eod_soc'] = h[H_EOD_SOC]
    if H_ACTIVE_POWER_PCT in h:
        out['active_power_pct'] = h[H_ACTIVE_POWER_PCT]
    if 66 in h:
        out['ac_charge_power_kw'] = h[66] / 10
    if 67 in h:
        out['ac_charge_soc_limit'] = h[67]
    if H_CT_OFFSET in h:
        out['ct_offset_w'] = h[H_CT_OFFSET] / CT_REG_PER_W
    return out
