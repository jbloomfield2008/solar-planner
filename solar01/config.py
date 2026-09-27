"""Configuration: built-in defaults overridden by a TOML file (/etc/solar01/config.toml).

Every default below is a value that was verified on the live system (see
README.md, "What we learned").  Unknown keys are rejected so typos fail loudly.
"""
from __future__ import annotations

import dataclasses as dc
import tomllib

BY_PATH = '/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-'


@dc.dataclass
class InverterConfig:
    port: str = BY_PATH + 'usb-0:1.2:1.0-port0'      # meter port (Modbus RTU master)
    baud: int = 19200
    slave: int = 1
    poll_s: float = 5.0                  # FlexBoss21 dislikes faster polling
    pv_avg_s: float = 60.0               # pv_power = rolling mean; pv_power_raw = instantaneous
    holding_refresh_s: float = 60.0
    # dynamic CT calibration, H119 (signed, LSB 0.1 W, firmware clamps +5000/-2500 units):
    # offset_w = slope * load_w + intercept.  slope 0 disables it.
    ct_cal_slope: float = 0.0
    ct_cal_intercept: float = 0.0
    ct_cal_hyst_w: float = 20.0
    ct_cal_min_write_s: float = 60.0     # EEPROM wear
    ct_cal_max_w: float = 500.0
    ct_cal_min_w: float = -250.0
    # standby (H21 bit 9 clear) is restored to normal when no standby hold has been
    # asserted by the planner for this long
    standby_watchdog_s: float = 1200.0
    qc_max_arm_min: int = 60             # refuse quick-charge countdowns longer than this


@dc.dataclass
class JkConfig:
    port: str = BY_PATH + 'usb-0:1.1:1.0-port0'
    baud: int = 115200
    poll_s: float = 5.0
    cells: int = 16                      # frames with a different cell count are rejected


@dc.dataclass
class EmulatorConfig:
    port: str = BY_PATH + 'usb-0:1.4:1.0-port0'      # inverter battery port (we are the slave)
    baud: int = 9600
    addrs: list[int] = dc.field(default_factory=lambda: [1])
    chg_volt: float = 55.4
    float_volt: float = 54.0
    chg_max_a: float = 100.0
    dischg_max_a: float = 150.0
    taper_start_mv: int = 3450
    taper_end_mv: int = 3550
    taper_reset_mv: int = 3400
    taper_min_a: float = 5.0
    chg_min_temp_c: float = 2.0
    chg_max_temp_c: float = 50.0
    cell_uv_mv: int = 2800
    stale_s: float = 30.0                # JK data older than this: charge forbidden
    dead_s: float = 300.0                # older than this: stop answering (BMS comm loss)
    soh: int = 100
    design_ah: float = 280.0
    current_sign: int = 1
    log_frames: bool = False


@dc.dataclass
class CoreConfig:
    socket: str = '/run/solar01/core.sock'           # or tcp://127.0.0.1:8765 (development)
    state_file: str = '/run/solar01/core-state.json'
    broadcast_s: float = 2.0
    pi_health_s: float = 30.0
    simulate: bool = False               # fake devices, for development without hardware
    inverter: InverterConfig = dc.field(default_factory=InverterConfig)
    jkbms: JkConfig = dc.field(default_factory=JkConfig)
    emulator: EmulatorConfig = dc.field(default_factory=EmulatorConfig)


@dc.dataclass
class SiteConfig:
    tz: str = 'America/Los_Angeles'
    lat: float = 32.96                   # ZIP 92129
    lon: float = -117.125


@dc.dataclass
class PlannerConfig:
    enabled_default: bool = True
    dry_run: bool = False
    # SDG&E super-off-peak windows, "start-end" hours
    weekday_sop: str = '0-6,10-14'
    weekend_sop: str = '0-14'
    on_peak: str = '16-21'               # display only: the planner itself only needs super-off-peak
    extra_holidays: list[str] = dc.field(default_factory=list)
    reserve_soc: float = 20.0            # a needed hold or charge is sized to keep SOC above this until the next window
    floor_soc: float = 15.0              # hard floor: the planner acts only if SOC would fall below it; outside super
                                         # off-peak it holds standby at the floor (grid feeds the house) until the next window
    max_soc: float = 100.0
    batt_kwh: float = 0.0                # 0 = JK capacity (Ah) x nominal_v
    nominal_v: float = 51.2
    chg_eff: float = 0.93
    dis_eff: float = 0.92
    load_margin: float = 1.15
    pv_margin: float = 0.8
    jit_margin_h: float = 0.75
    hyst_soc: float = 3.0
    pv_k_default: float = 2.0            # W of PV per W/m2 GHI until calibrated
    ac_chg_w_default: float = 2000.0
    pv_chg_max_kw: float = 2.1           # battery charge cap when the inverter limit is unknown
    dis_max_kw: float = 7.5
    history_days: int = 28
    profile_prior_days: float = 2.0
    live_pv_hours: float = 3.0
    live_pv_fade_h: float = 6.0
    live_pv_min: float = 0.5
    live_pv_max: float = 1.5
    live_pv_min_wh: float = 300.0
    tick_s: float = 60.0
    weather_refresh_s: float = 1800.0
    stale_s: float = 120.0
    write_min_interval_s: float = 30.0
    sim_step_s: int = 900
    hold_enabled: bool = True
    hold_clip_kwh: float = 0.5
    hold_max_pv_w: float = 100.0
    # holds and grid charges end this long before a super-off-peak window closes: after standby or a quick
    # charge the inverter takes ~5 minutes to return to normal (seen 2026-09-08 and 2026-09-14)
    window_exit_lead_min: float = 10.0
    projection_hours: float = 24.0
    qc_arm_min: int = 30
    qc_rearm_below: int = 10
    manual_max_h: float = 8.0            # longest manual charge (timed, or a SOC target's deadline)


@dc.dataclass
class MqttConfig:
    enabled: bool = False
    host: str = ''
    port: int = 1883
    username: str = ''
    password: str = ''
    discovery_prefix: str = 'homeassistant'
    publish_s: float = 5.0


@dc.dataclass
class WebConfig:
    enabled: bool = True
    host: str = '0.0.0.0'
    port: int = 80
    static_dir: str = ''                 # default: <release>/web


@dc.dataclass
class HubConfig:
    db: str = '/var/lib/solar01/solar01.db'
    legacy_db: str = '/var/lib/solar-tou/history.db'  # imported once into an empty database
    minute_retention_days: int = 90
    flush_s: float = 300.0
    site: SiteConfig = dc.field(default_factory=SiteConfig)
    planner: PlannerConfig = dc.field(default_factory=PlannerConfig)
    mqtt: MqttConfig = dc.field(default_factory=MqttConfig)
    web: WebConfig = dc.field(default_factory=WebConfig)


@dc.dataclass
class Config:
    log_level: str = 'INFO'
    core: CoreConfig = dc.field(default_factory=CoreConfig)
    hub: HubConfig = dc.field(default_factory=HubConfig)


def _coerce(current, value, name):
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise ValueError(f'{name} must be true or false')
        return value
    if isinstance(current, int):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ValueError(f'{name} must be an integer')
        return int(value)
    if isinstance(current, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'{name} must be a number')
        return float(value)
    if isinstance(current, str):
        if not isinstance(value, str):
            raise ValueError(f'{name} must be a string')
        return value
    if isinstance(current, list):
        if not isinstance(value, list):
            raise ValueError(f'{name} must be a list')
        return list(value)
    return value


def _apply(obj, data: dict, prefix: str = '') -> None:
    names = {f.name for f in dc.fields(obj)}
    for key, value in data.items():
        if key not in names:
            raise ValueError(f'unknown config key: {prefix}{key}')
        current = getattr(obj, key)
        if dc.is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f'{prefix}{key} must be a table')
            _apply(current, value, f'{prefix}{key}.')
        else:
            setattr(obj, key, _coerce(current, value, f'{prefix}{key}'))


def load(path: str | None = None) -> Config:
    cfg = Config()
    if path:
        with open(path, 'rb') as f:
            _apply(cfg, tomllib.load(f))
    return cfg


def from_dict(data: dict) -> Config:
    cfg = Config()
    _apply(cfg, data)
    return cfg


def redacted(cfg: Config) -> dict:
    d = dc.asdict(cfg)
    if d['hub']['mqtt'].get('password'):
        d['hub']['mqtt']['password'] = '***'
    return d
