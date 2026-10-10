"""Settings the console may change at run time, with the bounds each is held to.

The TOML file (config.py) stays the base: a console change is an override on top of it, and
clearing the override brings the file value back.  Keys under ``core.`` belong to the core,
which checks them again, applies them and keeps them in its own state file, so it never depends
on the hub for its settings.  Everything else is the hub's, kept in the database ``kv`` table.

Left out on purpose: serial ports and addresses, the BMS emulator's current, voltage and temperature
limits (the cell undervoltage cutoff is in, held to a narrow range), the core
safety limits (standby watchdog, longest quick-charge countdown), the MQTT connection, the web
listener, the time zone, and the planner's internal tuning.  Those stay in the file.
"""
from __future__ import annotations

import copy
import dataclasses as dc
import datetime as dt
import math
import re

from .config import Config, _coerce


@dc.dataclass(frozen=True)
class Setting:
    key: str                          # dotted path into Config
    group: str
    label: str
    unit: str = ''
    min: float | None = None
    max: float | None = None
    step: float | None = None
    help: str = ''

    @property
    def owner(self) -> str:
        return 'core' if self.key.startswith('core.') else 'hub'


SAMPLING = 'Sampling'
CT = 'CT calibration'
BATTERY = 'Battery and reserve'
FORECAST = 'Forecast margins'
ACTIONS = 'Holds and charges'
CALENDAR = 'Time-of-use calendar'
SITE = 'Site and history'

SETTINGS = (
    Setting('core.jkbms.poll_s', SAMPLING, 'Battery sample time', 's', 1, 10, 0.5,
            'How often the BMS is read. The BMS bridge always serves the latest sample.'),
    Setting('core.inverter.poll_s', SAMPLING, 'Inverter sample time', 's', 5, 60, 1,
            'The FlexBoss21 does not cope with polling faster than every 5 s.'),
    Setting('core.inverter.pv_avg_s', SAMPLING, 'Solar power averaging', 's', 0, 600, 5,
            'The solar power shown is the mean over this long.'),
    Setting('core.inverter.holding_refresh_s', SAMPLING, 'Inverter settings re-read', 's', 10, 600, 5),
    Setting('core.inverter.ct_cal_slope', CT, 'Slope', '× load', -0.2, 0.2, 0.001,
            'offset = slope × load + offset at no load. A slope of 0 turns calibration off.'),
    Setting('core.inverter.ct_cal_intercept', CT, 'Offset at no load', 'W', -250, 500, 1),
    Setting('core.inverter.ct_cal_min_w', CT, 'Lowest offset', 'W', -250, 500, 1),
    Setting('core.inverter.ct_cal_max_w', CT, 'Highest offset', 'W', -250, 500, 1,
            '0 never biases the reading toward export.'),
    Setting('core.inverter.ct_cal_hyst_w', CT, 'Write when the target moves by', 'W', 1, 200, 1),
    Setting('core.inverter.ct_cal_min_write_s', CT, 'Shortest time between writes', 's', 60, 3600, 10,
            'Each write goes to the inverter EEPROM.'),
    Setting('hub.planner.floor_soc', BATTERY, 'Floor', '%', 5, 50, 1,
            'A buffer under the reserve for when the house uses more than forecast; the planner does not plan to it. '
            'At the floor outside super off-peak, the grid carries the house until the next window.'),
    Setting('hub.planner.reserve_soc', BATTERY, 'Reserve', '%', 5, 60, 1,
            'In a super off-peak window the planner holds or charges whenever the forecast would take the battery '
            'below this before the next window, and sizes it to stay above.'),
    Setting('hub.planner.max_soc', BATTERY, 'Highest charge target', '%', 50, 100, 1),
    Setting('core.emulator.cell_uv_mv', BATTERY, 'Stop discharge at cell voltage', 'mV', 2500, 3000, 10,
            'Any cell at or below this: the battery stops discharging (solar still charges it, the grid carries '
            'the rest).'),
    Setting('core.emulator.cell_uv_restore_mv', BATTERY, 'Allow discharge again at', 'mV', 2700, 3400, 10,
            'Discharge resumes once every cell is at or above this.'),
    Setting('hub.planner.batt_kwh', BATTERY, 'Battery capacity', 'kWh', 0, 100, 0.1,
            '0 uses the BMS capacity × nominal voltage.'),
    Setting('hub.planner.nominal_v', BATTERY, 'Nominal voltage', 'V', 40, 60, 0.1),
    Setting('hub.planner.load_margin', FORECAST, 'Load forecast factor', '×', 0.5, 2, 0.01,
            'Expected home use is multiplied by this.'),
    Setting('hub.planner.pv_margin', FORECAST, 'Solar forecast factor', '×', 0.3, 1.5, 0.01,
            'Forecast solar is multiplied by this.'),
    Setting('hub.planner.chg_eff', FORECAST, 'Charge efficiency', '', 0.5, 1, 0.01),
    Setting('hub.planner.dis_eff', FORECAST, 'Discharge efficiency', '', 0.5, 1, 0.01),
    Setting('hub.planner.jit_margin_h', FORECAST, 'Start grid charges early by', 'h', 0, 4, 0.05),
    Setting('hub.planner.hyst_soc', FORECAST, 'Hysteresis', '%', 0, 10, 0.5),
    Setting('hub.planner.history_days', FORECAST, 'Load profile history', 'days', 3, 90, 1),
    Setting('hub.planner.hold_enabled', ACTIONS, 'Standby holds', help='Off: the planner only grid-charges.'),
    Setting('hub.planner.window_exit_lead_min', ACTIONS, 'End before a window closes', 'min', 0, 60, 1,
            'The inverter takes about 5 minutes to return to normal after a hold or charge.'),
    Setting('hub.planner.qc_arm_min', ACTIONS, 'Quick-charge countdown', 'min', 5, 60, 1,
            'The inverter stops a charge by itself when the countdown runs out.'),
    Setting('hub.planner.qc_rearm_below', ACTIONS, 'Re-arm when fewer remain', 'min', 1, 59, 1),
    Setting('hub.planner.manual_max_h', ACTIONS, 'Longest manual charge', 'h', 0.5, 24, 0.5),
    Setting('hub.planner.pv_chg_max_kw', ACTIONS, 'Battery charge cap if unknown', 'kW', 0.3, 20, 0.1),
    Setting('hub.planner.dis_max_kw', ACTIONS, 'Battery discharge cap', 'kW', 0.5, 20, 0.1),
    Setting('hub.planner.dry_run', ACTIONS, 'Dry run', help='Log decisions without writing to the inverter.'),
    Setting('hub.planner.weekday_sop', CALENDAR, 'Weekday super off-peak', 'hours',
            help='Hour ranges, for example 0-6,10-14.'),
    Setting('hub.planner.weekend_sop', CALENDAR, 'Weekend and holiday super off-peak', 'hours'),
    Setting('hub.planner.on_peak', CALENDAR, 'On-peak', 'hours', help='Only used for display and cost.'),
    Setting('hub.planner.extra_holidays', CALENDAR, 'Extra holidays',
            help='Dates as YYYY-MM-DD, separated by commas. Federal holidays are built in.'),
    Setting('hub.site.lat', SITE, 'Latitude', '°', -90, 90, 0.001, 'For the solar forecast.'),
    Setting('hub.site.lon', SITE, 'Longitude', '°', -180, 180, 0.001),
    Setting('hub.minute_retention_days', SITE, 'Keep minute history for', 'days', 7, 3650, 1),
    Setting('hub.mqtt.publish_s', SITE, 'Home Assistant update interval', 's', 1, 300, 1),
)
BY_KEY = {s.key: s for s in SETTINGS}
CALENDAR_KEYS = {'hub.planner.weekday_sop', 'hub.planner.weekend_sop', 'hub.planner.extra_holidays'}
_WINDOWS = {'hub.planner.weekday_sop', 'hub.planner.weekend_sop', 'hub.planner.on_peak'}
_RANGE = re.compile(r'^(\d{1,2})-(\d{1,2})$')


def get(cfg, key: str):
    obj = cfg
    for part in key.split('.'):
        obj = getattr(obj, part)
    return obj


def put(cfg, key: str, value) -> None:
    *path, name = key.split('.')
    obj = cfg
    for part in path:
        obj = getattr(obj, part)
    setattr(obj, name, value)


def _windows(value: str, name: str, required: bool) -> str:
    out = []
    for part in value.replace(' ', '').split(','):
        if not part:
            continue
        m = _RANGE.match(part)
        if not m or not 0 <= int(m[1]) < int(m[2]) <= 24:
            raise ValueError(f'{name}: "{part}" is not an hour range like 10-14 (0 to 24)')
        out.append((int(m[1]), int(m[2])))
    if required and not out:
        raise ValueError(f'{name} needs at least one hour range')
    out.sort()
    if any(a[1] > b[0] for a, b in zip(out, out[1:])):
        raise ValueError(f'{name}: hour ranges overlap')
    return ','.join(f'{a}-{b}' for a, b in out)


def validate(key: str, value, current):
    """The value, coerced to the type of ``current`` and checked against the bounds; ValueError otherwise."""
    s = BY_KEY.get(key)
    if s is None:
        raise ValueError(f'{key} cannot be changed from the console')
    value = _coerce(current, value, s.label)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or (s.min is not None and value < s.min) or (s.max is not None and value > s.max):
            raise ValueError(f'{s.label} must be {s.min:g} to {s.max:g}{" " + s.unit if s.unit else ""}')
    if key in _WINDOWS:
        value = _windows(value, s.label, key != 'hub.planner.on_peak')
    if key == 'hub.planner.extra_holidays':
        try:
            value = sorted({dt.date.fromisoformat(str(v).strip()).isoformat() for v in value})
        except ValueError:
            raise ValueError(f'{s.label}: use dates like 2026-12-24') from None
    return value


def check(cfg, owner: str) -> str | None:
    """Rules across settings, on the effective values; None when they hold."""
    def g(key):
        return get(cfg, key)
    if owner == 'core':
        if g('core.inverter.ct_cal_min_w') > g('core.inverter.ct_cal_max_w'):
            return 'the lowest CT offset must not be above the highest'
        if g('core.emulator.cell_uv_restore_mv') <= g('core.emulator.cell_uv_mv'):
            return 'discharge must be allowed again at a higher cell voltage than it stops at'
        return None
    if not g('hub.planner.floor_soc') <= g('hub.planner.reserve_soc') <= g('hub.planner.max_soc'):
        return 'the floor must not be above the reserve, nor the reserve above the highest charge target'
    if g('hub.planner.qc_arm_min') > g('core.inverter.qc_max_arm_min'):
        return f'the quick-charge countdown must not exceed {g("core.inverter.qc_max_arm_min")} min (core limit)'
    if g('hub.planner.qc_rearm_below') >= g('hub.planner.qc_arm_min'):
        return 'the re-arm point must be below the quick-charge countdown'
    return None


class Overrides:
    """Console overrides for one owner ('core' or 'hub') on top of the file configuration."""

    def __init__(self, cfg, owner: str):
        self.cfg = cfg
        self.owner = owner
        self.file = {s.key: get(cfg, s.key) for s in SETTINGS if s.owner == owner}
        self.values: dict = {}

    def load(self, stored: dict | None) -> list[str]:
        """Apply stored overrides, skipping any that no longer validate.  Returns what was skipped."""
        values, skipped = {}, []
        for key, value in (stored or {}).items():
            try:
                if key not in self.file:
                    raise ValueError('not a setting')
                values[key] = validate(key, value, self.file[key])
            except (ValueError, TypeError) as e:
                skipped.append(f'{key} ({e})')
        err = check(self._with(values), self.owner) if values else None
        if err:
            skipped.append(f'all of them ({err})')
            values = {}
        self.values = values
        self._apply(self.cfg, values)
        return skipped

    def update(self, changes: dict, dry_run: bool = False) -> dict:
        """Validate and apply changes (None clears an override), all or nothing.  Returns {key: (old, new)}.
        A dry run only validates.  The live configuration is only touched once everything checks out."""
        values = dict(self.values)
        for key, value in changes.items():
            if key not in self.file:
                raise ValueError(f'{key} is not a {self.owner} setting' if key in BY_KEY
                                 else f'{key} cannot be changed from the console')
            if value is None:
                values.pop(key, None)
            else:
                values[key] = validate(key, value, self.file[key])
        trial = self._with(values)
        err = check(trial, self.owner)
        if err:
            raise ValueError(err)
        changed = {k: (get(self.cfg, k), get(trial, k)) for k in self.file if get(self.cfg, k) != get(trial, k)}
        if not dry_run:
            self.values = values
            self._apply(self.cfg, values)
        return changed

    def _with(self, values: dict):
        trial = copy.deepcopy(self.cfg)
        self._apply(trial, values)
        return trial

    def _apply(self, cfg, values: dict) -> None:
        for key, file_value in self.file.items():
            put(cfg, key, values.get(key, file_value))

    def state(self) -> dict:
        return {'values': {k: get(self.cfg, k) for k in self.file}, 'file': dict(self.file),
                'overrides': sorted(self.values)}


_TYPES = {bool: 'bool', int: 'int', float: 'float', str: 'str', list: 'list'}


def items(states: dict) -> list[dict]:
    """Every setting for the console.  states: owner -> Overrides.state(), or None while unknown
    (the hub has not heard from the core yet)."""
    defaults = Config()
    out = []
    for s in SETTINGS:
        st = states.get(s.owner)
        out.append({**dc.asdict(s), 'owner': s.owner, 'type': _TYPES[type(get(defaults, s.key))],
                    'value': st['values'].get(s.key) if st else None, 'file': st['file'].get(s.key) if st else None,
                    'overridden': bool(st) and s.key in st['overrides'], 'available': st is not None})
    return out


def describe(changes: dict) -> str:
    def fmt(key, v):
        s = BY_KEY[key]
        if isinstance(v, bool):
            return 'on' if v else 'off'
        if isinstance(v, list):
            return ', '.join(v) or 'none'
        return f'{v:g} {s.unit}'.rstrip() if isinstance(v, (int, float)) else str(v)
    return '; '.join(f'{BY_KEY[k].label} {fmt(k, a)} -> {fmt(k, b)}' for k, (a, b) in changes.items())
