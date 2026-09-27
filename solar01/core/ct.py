"""Dynamic CT calibration through H119 (CT power offset, signed, LSB 0.1 W).

The FlexBoss CT reading carries a load-dependent error.  The fit from meter readings on
2026-08-21 was about 4.5 % over-read plus a ~60 W import bias; with the user's 40 W import
buffer that became offset_w = -0.045 x load_w + 20.  The core writes the offset that
cancels the predicted error, clamped to the user's policy (ct_cal_max_w = 0: never bias
toward export) and to the firmware range (+500 / -250 W).  It writes only when the target
has moved by the hysteresis, and not more often than ct_cal_min_write_s (EEPROM wear).
"""
from __future__ import annotations

import time

from ..devices import modbus
from ..devices.flexboss import CT_REG_MAX, CT_REG_MIN, CT_REG_PER_W
from .safety import ct_offset_target, ct_should_write


def _watts(reg: int | None) -> float | None:
    return None if reg is None else reg / CT_REG_PER_W


class CtCalibrator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.current_reg: int | None = None      # what the inverter holds (snapshot or our last write)
        self.target_reg: int | None = None
        self.load_w: float | None = None
        self.last_write_mono: float | None = None
        self.last_write_ts: float | None = None
        self.last_write_reg: int | None = None
        self.writes = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.ct_cal_slope != 0

    def observe_register(self, value) -> None:
        """Take the value read back from the inverter as the truth."""
        if value is not None:
            self.current_reg = int(value)

    def step(self, load_w: float, write, now_mono: float) -> str | None:
        """One poll: 'written', 'failed' or None.  write(register_value) performs the Modbus write;
        serial-level OSErrors propagate so the caller can reopen the port."""
        self.load_w = float(load_w)
        self.target_reg = ct_offset_target(self.load_w, self.cfg)
        if not self.enabled:
            return None
        since = None if self.last_write_mono is None else now_mono - self.last_write_mono
        if not ct_should_write(self.target_reg, self.current_reg, since, self.cfg):
            return None
        try:
            write(self.target_reg)
        except (modbus.LinkError, modbus.ModbusException) as e:
            self.last_error = str(e)
            return 'failed'
        self.current_reg = self.last_write_reg = self.target_reg
        self.last_write_mono, self.last_write_ts = now_mono, time.time()
        self.writes += 1
        self.last_error = None
        return 'written'

    def state(self, now_mono: float | None = None) -> dict:
        now_mono = time.monotonic() if now_mono is None else now_mono
        c = self.cfg
        pending = bool(self.enabled and self.current_reg is not None and self.target_reg is not None
                       and abs(self.target_reg - self.current_reg) >= c.ct_cal_hyst_w * CT_REG_PER_W)
        next_in = None
        if pending and self.last_write_mono is not None:
            next_in = round(max(0.0, c.ct_cal_min_write_s - (now_mono - self.last_write_mono)), 1)
        return {
            'enabled': self.enabled, 'register': self.current_reg, 'offset_w': _watts(self.current_reg),
            'target_w': _watts(self.target_reg), 'load_w': self.load_w, 'pending': pending, 'next_write_in_s': next_in,
            'writes': self.writes, 'last_write_ts': None if self.last_write_ts is None else round(self.last_write_ts, 1),
            'last_write_w': _watts(self.last_write_reg), 'last_error': self.last_error,
            'slope': c.ct_cal_slope, 'intercept_w': c.ct_cal_intercept, 'min_w': c.ct_cal_min_w, 'max_w': c.ct_cal_max_w,
            'hyst_w': c.ct_cal_hyst_w, 'min_write_s': c.ct_cal_min_write_s,
            'firmware_min_w': CT_REG_MIN / CT_REG_PER_W, 'firmware_max_w': CT_REG_MAX / CT_REG_PER_W,
        }
