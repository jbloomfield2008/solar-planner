"""Home Assistant MQTT bridge.

Same state topics, discovery topics and unique_ids as the scripts this app replaces
(solar_mqtt.py, bms_emu.py, solar_tou.py), so Home Assistant entities, their history and
the "Solar & Battery" dashboard carry on unchanged.  The bridge is an output only, apart
from the "Planner enabled" switch.  Nothing else in the app waits on it: when the broker
is unreachable it just retries in the background.

The inverter holding-register MQTT write service is gone: no MQTT client can write
inverter registers any more.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .. import __version__

log = logging.getLogger('solar01.mqtt')

T_AVAIL = 'solar/solar01/availability'           # LWT of the single MQTT connection
T_INV = 'solar/flexboss21/state'
T_JK = 'solar/jkbms/state'
T_PI = 'solar/solar01/state'
T_EMU = 'solar/bmsemu/state'
T_EMU_AVAIL = 'solar/bmsemu/availability'
T_TOU = 'solar/tou/state'
T_TOU_AVAIL = 'solar/tou/availability'
T_TOU_SET = 'solar/tou/set/'
RETIRED_RETAINED = ('homeassistant/binary_sensor/solar01_tou_ac_charge_function/config',
                    'solar/flexboss21/holding')

INV_DEVICE = {'identifiers': ['flexboss21_4563E00048'], 'name': 'FlexBoss21', 'manufacturer': 'EG4', 'model': 'FlexBoss21'}
BMS_DEVICE = {'identifiers': ['jkbms_200874'], 'name': 'Battery JK BMS', 'manufacturer': 'JiKong',
              'model': 'JK BMS 16S 280Ah'}
PI_DEVICE = {'identifiers': ['solar01_pi'], 'name': 'solar01 Pi', 'manufacturer': 'Raspberry Pi', 'model': 'Pi 4'}
EMU_DEVICE = {'identifiers': ['solar01_bmsemu'], 'name': 'BMS emulator', 'manufacturer': 'DIY',
              'model': 'Lux-protocol emulator (JK BMS bridge)', 'via_device': 'solar01_pi'}
TOU_DEVICE = {'identifiers': ['solar01_tou'], 'name': 'TOU charge planner', 'manufacturer': 'solar01',
              'model': 'solar01 hub'}

# key: (name, unit, device_class, state_class)
INV_SENSORS = {
    'mode': ('Mode', None, None, None),
    'pv_power': ('PV power', 'W', 'power', 'measurement'),
    'pv_power_raw': ('PV power raw', 'W', 'power', 'measurement'),
    'pv1_voltage': ('PV1 voltage', 'V', 'voltage', 'measurement'),
    'battery_power': ('Battery power', 'W', 'power', 'measurement'),
    'battery_voltage': ('Battery voltage (inverter)', 'V', 'voltage', 'measurement'),
    'soc': ('Battery SOC (inverter)', '%', 'battery', 'measurement'),
    'grid_voltage': ('Grid voltage', 'V', 'voltage', 'measurement'),
    'grid_voltage_l1': ('Grid voltage L1', 'V', 'voltage', 'measurement'),
    'grid_voltage_l2': ('Grid voltage L2', 'V', 'voltage', 'measurement'),
    'grid_frequency': ('Grid frequency', 'Hz', 'frequency', 'measurement'),
    'inverter_power': ('Inverter power', 'W', 'power', 'measurement'),
    'load_power': ('Load power', 'W', 'power', 'measurement'),
    'grid_import_power': ('Grid import power', 'W', 'power', 'measurement'),
    'grid_export_power': ('Grid export power', 'W', 'power', 'measurement'),
    'ct_current': ('Grid CT current', 'A', 'current', 'measurement'),
    'load_energy_total': ('Load energy total', 'kWh', 'energy', 'total_increasing'),
    'pv_energy_today': ('PV energy today', 'kWh', 'energy', 'total_increasing'),
    'load_energy_today': ('Load energy today', 'kWh', 'energy', 'total_increasing'),
    'charge_energy_today': ('Charge energy today', 'kWh', 'energy', 'total_increasing'),
    'discharge_energy_today': ('Discharge energy today', 'kWh', 'energy', 'total_increasing'),
    'import_energy_today': ('Import energy today', 'kWh', 'energy', 'total_increasing'),
    'export_energy_today': ('Export energy today', 'kWh', 'energy', 'total_increasing'),
    'temp_internal': ('Inverter temp internal', '°C', 'temperature', 'measurement'),
    'temp_radiator1': ('Inverter temp radiator 1', '°C', 'temperature', 'measurement'),
    'ct_power_offset': ('CT power offset', 'W', 'power', 'measurement'),
    'ct_power_offset_target': ('CT offset target', 'W', 'power', 'measurement'),
    'quick_charge_remaining': ('Quick charge remaining', 's', 'duration', 'measurement'),
}
BMS_SENSORS = {
    'voltage': ('Battery voltage', 'V', 'voltage', 'measurement'),
    'current': ('Battery current', 'A', 'current', 'measurement'),
    'power': ('Battery power (BMS)', 'W', 'power', 'measurement'),
    'soc': ('Battery SOC', '%', 'battery', 'measurement'),
    'remaining_capacity': ('Remaining capacity', 'Ah', None, 'measurement'),
    'capacity': ('Nominal capacity', 'Ah', None, None),
    'cycle_count': ('Cycle count', None, None, 'total_increasing'),
    'temp_mos': ('BMS MOS temp', '°C', 'temperature', 'measurement'),
    'temp_1': ('Battery temp 1', '°C', 'temperature', 'measurement'),
    'temp_2': ('Battery temp 2', '°C', 'temperature', 'measurement'),
    'cell_voltage_min': ('Cell voltage min', 'mV', 'voltage', 'measurement'),
    'cell_voltage_max': ('Cell voltage max', 'mV', 'voltage', 'measurement'),
    'cell_voltage_delta': ('Cell voltage delta', 'mV', 'voltage', 'measurement'),
    'warning_bits': ('BMS warning bits', None, None, None),
}
EMU_SENSORS = {
    'max_charge_current': ('Max charge current', 'A', 'current', 'measurement'),
    'max_discharge_current': ('Max discharge current', 'A', 'current', 'measurement'),
    'charge_voltage': ('Charge voltage request', 'V', 'voltage', 'measurement'),
    'poll_age': ('Inverter poll age', 's', 'duration', 'measurement'),
    'polls': ('Polls answered', None, None, 'total_increasing'),
    'jk_age': ('JK data age', 's', 'duration', 'measurement'),
    'limit_reason': ('Charge limit reason', None, None, None),
}
# key: (name, unit, device_class, icon)
TOU_SENSORS = {
    'target_soc': ('Target SOC', '%', None, 'mdi:battery-arrow-up'),
    'grid_kwh': ('Grid charge needed', 'kWh', 'energy', None),
    'hours_needed': ('Charge hours needed', 'h', None, 'mdi:timer-sand'),
    'action': ('Decision', None, None, 'mdi:state-machine'),
    'reason': ('Decision reason', None, None, 'mdi:text'),
    'next_window': ('Next super off-peak', None, 'timestamp', None),
    'window_end': ('Window end', None, 'timestamp', None),
    'hold_start': ('Standby hold starts', None, 'timestamp', None),
    'hold_end': ('Standby hold ends', None, 'timestamp', None),
    'start_at': ('Planned charge start', None, 'timestamp', None),
    'forecast_load_kwh': ('Forecast load to horizon', 'kWh', 'energy', None),
    'forecast_pv_kwh': ('Forecast PV to horizon', 'kWh', 'energy', None),
    'forecast_min_soc': ('Forecast min SOC (no grid charge)', '%', None, 'mdi:battery-alert'),
    'projected_min_soc': ('Projected min SOC (with plan)', '%', None, 'mdi:battery-clock'),
    'projected_soc_window_end': ('Projected SOC at window end', '%', None, 'mdi:battery-clock'),
    'projected_soc_next_window': ('Projected SOC at next window', '%', None, 'mdi:battery-clock'),
    'lowest_24h': ('Projected min SOC next 24 h', '%', None, 'mdi:battery-clock'),
    'pv_k': ('PV yield per W/m2', 'W', None, 'mdi:solar-power'),
    'pv_live_scale': ('PV live correction', 'x', None, 'mdi:sun-wireless'),
    'charge_kw': ('Grid charge rate', 'kW', 'power', None),
    'chg_cap_kw': ('Battery charge cap', 'kW', 'power', None),
    'soc_source': ('SOC source', None, None, 'mdi:battery-sync'),
    'ghi_today_kwh': ('Irradiance today', 'kWh/m²', None, 'mdi:weather-sunny'),
    'ghi_tomorrow_kwh': ('Irradiance tomorrow', 'kWh/m²', None, 'mdi:weather-sunny'),
    'pv_forecast_today_kwh': ('PV forecast today', 'kWh', 'energy', 'mdi:solar-power-variant'),
    'pv_forecast_tomorrow_kwh': ('PV forecast tomorrow', 'kWh', 'energy', 'mdi:solar-power-variant'),
    'history_days': ('Load history days', 'd', None, 'mdi:history'),
    'last_write': ('Last inverter write', None, None, 'mdi:pencil'),
    'quick_charge_minutes': ('Quick charge armed', 'min', None, 'mdi:timer-outline'),
    'warning': ('Planner warning', None, None, 'mdi:alert'),
    'net_now_kw': ('Forecast PV minus load now', 'kW', 'power', None),
    'clipped_kwh_if_hold': ('PV clipped if holding', 'kWh', 'energy', None),
}
TOU_BINARY = {'in_sop': 'Super off-peak now', 'ac_charging': 'Charging from grid', 'holiday': 'Holiday/weekend tariff',
              'quick_charge_active': 'Quick charge active', 'hold': 'Standby hold wanted',
              'standby': 'Inverter in standby', 'night': 'Sun down (no PV expected)'}


def _numeric(cfg: dict, unit, dclass, sclass) -> dict:
    if unit:
        cfg['unit_of_measurement'] = unit
    if dclass:
        cfg['device_class'] = dclass
    if sclass:
        cfg['state_class'] = sclass
    return cfg


def _avail(*extra: str) -> dict:
    if not extra:
        return {'availability_topic': T_AVAIL}
    return {'availability': [{'topic': T_AVAIL}] + [{'topic': t} for t in extra], 'availability_mode': 'all'}


def discovery_messages(prefix: str = 'homeassistant') -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []

    def sensor(node, key, meta, topic, device, extra=None, avail=()):
        name, unit, dclass, sclass = meta
        c = _numeric({'name': name, 'unique_id': f'{node}_{key}', 'state_topic': topic,
                      'value_template': '{{ value_json.%s }}' % key, 'device': device, 'expire_after': 60,
                      **_avail(*avail)}, unit, dclass, sclass)
        c.update(extra or {})
        out.append((f'{prefix}/sensor/{node}_{key}/config', c))

    for key, meta in INV_SENSORS.items():
        sensor('solar01_fb21', key, meta, T_INV, INV_DEVICE)
    for key, meta in BMS_SENSORS.items():
        extra = {'json_attributes_topic': T_JK, 'json_attributes_template': '{{ value_json.cells | tojson }}'} \
            if key == 'cell_voltage_delta' else None
        sensor('solar01_jkbms', key, meta, T_JK, BMS_DEVICE, extra)
    for key, name in (('charge_mos_on', 'Charge MOS'), ('discharge_mos_on', 'Discharge MOS'), ('balancing', 'Balancing')):
        out.append((f'{prefix}/binary_sensor/solar01_jkbms_{key}/config',
                    {'name': name, 'unique_id': f'solar01_jkbms_{key}', 'state_topic': T_JK,
                     'value_template': '{{ value_json.%s }}' % key, 'payload_on': 'True', 'payload_off': 'False',
                     'device': BMS_DEVICE, **_avail()}))
    sensor('solar01_pi', 'cpu_temp', ('CPU temperature', '°C', 'temperature', 'measurement'), T_PI, PI_DEVICE)
    sensor('solar01_pi', 'throttled_raw', ('Throttled flags', None, None, None), T_PI, PI_DEVICE)
    sensor('solar01_pi', 'inverter_battery_type', ('Inverter battery type', None, None, None), T_PI, PI_DEVICE)
    for key, name, dclass in (('undervoltage_now', 'Undervoltage', 'problem'), ('throttled_now', 'CPU throttled', 'problem'),
                              ('undervoltage_since_boot', 'Undervoltage since boot', 'problem'),
                              ('throttled_since_boot', 'Throttled since boot', 'problem'),
                              ('core_connected', 'Core process connected', 'connectivity'),
                              ('emulator_polling', 'Inverter polling BMS emulator', 'running')):
        out.append((f'{prefix}/binary_sensor/solar01_pi_{key}/config',
                    {'name': name, 'unique_id': f'solar01_pi_{key}', 'state_topic': T_PI,
                     'value_template': '{{ value_json.%s }}' % key, 'payload_on': 'True', 'payload_off': 'False',
                     'device_class': dclass, 'device': PI_DEVICE, **_avail()}))
    for key, meta in EMU_SENSORS.items():
        sensor('solar01_bmsemu', key, meta, T_EMU, EMU_DEVICE, avail=(T_EMU_AVAIL,))
    for key, name in (('charge_ok', 'Charge allowed'), ('discharge_ok', 'Discharge allowed')):
        out.append((f'{prefix}/binary_sensor/solar01_bmsemu_{key}/config',
                    {'name': name, 'unique_id': f'solar01_bmsemu_{key}', 'state_topic': T_EMU,
                     'value_template': '{{ "ON" if value_json.%s else "OFF" }}' % key, 'device': EMU_DEVICE,
                     'expire_after': 60, **_avail(T_EMU_AVAIL)}))
    for key, (name, unit, dclass, icon) in TOU_SENSORS.items():
        c = _numeric({'name': name, 'unique_id': f'solar01_tou_{key}', 'state_topic': T_TOU,
                      'value_template': '{%% if value_json.%s is defined and value_json.%s is not none %%}'
                                        '{{ value_json.%s }}{%% else %%}None{%% endif %%}' % (key, key, key),
                      'device': TOU_DEVICE, 'expire_after': 600, **_avail(T_TOU_AVAIL)}, unit, dclass, None)
        if icon:
            c['icon'] = icon
        if key == 'action':
            c['json_attributes_topic'] = T_TOU
            c['json_attributes_template'] = ('{{ {"trace": value_json.trace, "window": value_json.window, '
                                             '"horizon": value_json.horizon, "warning": value_json.warning} | tojson }}')
        out.append((f'{prefix}/sensor/solar01_tou_{key}/config', c))
    for key, name in TOU_BINARY.items():
        out.append((f'{prefix}/binary_sensor/solar01_tou_{key}/config',
                    {'name': name, 'unique_id': f'solar01_tou_{key}', 'state_topic': T_TOU,
                     'value_template': '{{ value_json.%s }}' % key, 'payload_on': 'True', 'payload_off': 'False',
                     'device': TOU_DEVICE, 'expire_after': 600, **_avail(T_TOU_AVAIL)}))
    out.append((f'{prefix}/switch/solar01_tou_enabled/config',
                 {'name': 'Planner enabled', 'unique_id': 'solar01_tou_enabled', 'state_topic': T_TOU,
                  'value_template': '{{ "ON" if value_json.enabled else "OFF" }}', 'command_topic': T_TOU_SET + 'enabled',
                  'payload_on': 'ON', 'payload_off': 'OFF', 'device': TOU_DEVICE, 'icon': 'mdi:brain',
                  **_avail(T_TOU_AVAIL)}))
    return out


def emulator_payload(emu: dict, jk_age) -> dict:
    regs = emu.get('regs')
    return {'max_charge_current': emu.get('max_charge_a'), 'max_discharge_current': emu.get('max_discharge_a'),
            'charge_voltage': emu.get('charge_voltage'), 'charge_ok': bool(emu.get('charge_ok')),
            'discharge_ok': bool(emu.get('discharge_ok')), 'limit_reason': emu.get('limit_reason'),
            'jk_age': jk_age, 'poll_age': emu.get('poll_age_s'), 'polls': emu.get('polls'),
            'writes': emu.get('writes'), 'crc_errors': emu.get('crc_errors'), 'other_addrs': emu.get('other_addrs'),
            'cell_max': regs[10] if regs else None, 'regs': regs, 'polling': emu.get('polling')}


class MqttBridge:
    def __init__(self, cfg, store, loop: asyncio.AbstractEventLoop):
        import paho.mqtt.client as mqtt
        self.cfg = cfg.hub.mqtt
        self.store = store
        self.loop = loop
        self.connected = False
        self.last_error: str | None = None
        self.published = 0
        self._last_tou = 0.0
        self._last_plan_ts = None
        self._last_pi: dict = {}
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='solar01-hub')
        except AttributeError:
            self.client = mqtt.Client(client_id='solar01-hub')
        if self.cfg.username:
            self.client.username_pw_set(self.cfg.username, self.cfg.password)
        self.client.will_set(T_AVAIL, 'offline', retain=True)
        self.client.reconnect_delay_set(2, 30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_connect_fail = self._on_connect_fail

    def start(self) -> None:
        self.client.connect_async(self.cfg.host, self.cfg.port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            if self.connected:
                self.client.publish(T_AVAIL, 'offline', retain=True).wait_for_publish(2)
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.client.loop_stop()

    # -- paho callbacks (network thread) ------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, 'is_failure', False):
            self.connected, self.last_error = False, f'refused: {reason_code}'
            return
        self.connected, self.last_error = True, None
        log.info('MQTT connected to %s', self.cfg.host)
        client.subscribe(T_TOU_SET + '#')
        client.subscribe(f'{self.cfg.discovery_prefix}/status')
        client.publish(T_AVAIL, 'online', retain=True)
        self.publish_discovery()

    def _on_disconnect(self, client, userdata, *args):
        if self.connected:
            log.warning('MQTT disconnected')
        self.connected = False

    def _on_connect_fail(self, client, userdata):
        self.last_error = 'connection failed'

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors='replace').strip()
        if msg.topic == T_TOU_SET + 'enabled' and self.store.planner is not None:
            enabled = payload.upper() in ('ON', '1', 'TRUE')
            asyncio.run_coroutine_threadsafe(self.store.planner.set_enabled(enabled, 'Home Assistant'), self.loop)
        elif msg.topic == f'{self.cfg.discovery_prefix}/status' and payload == 'online':
            self.publish_discovery()                  # Home Assistant restarted

    # -- publishing -------------------------------------------------------------------------------------
    def pub(self, topic: str, payload, retain: bool = False) -> None:
        data = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        self.client.publish(topic, data, retain=retain)
        self.published += 1

    def publish_discovery(self) -> None:
        for topic, payload in discovery_messages(self.cfg.discovery_prefix):
            self.pub(topic, payload, retain=True)
        for topic in RETIRED_RETAINED:
            self.pub(topic, '', retain=True)

    def publish_states(self) -> None:
        st = self.store
        s = st.core_state
        self.pub(T_AVAIL, 'online', retain=True)       # re-assert: a stale LWT from a taken-over session can stick
        core_ok = bool(s) and bool(st.core_link and st.core_link.connected) and st.link_age() < 15
        if core_ok:
            inv, jk = st.section('inverter'), st.section('jk')
            if inv.get('data') and (inv.get('age_s') or 0) < 30:
                self.pub(T_INV, inv['data'])
            if jk.get('data') and (jk.get('age_s') or 0) < 30:
                self.pub(T_JK, jk['data'])
            emu = s.get('emulator') or {}
            self.pub(T_EMU, emulator_payload(emu, jk.get('age_s')))
            self.pub(T_EMU_AVAIL, 'online', retain=True)
            self._last_pi = dict(s.get('pi') or {})
            pi = dict(self._last_pi, core_connected=True, emulator_polling=bool(emu.get('polling')),
                      inverter_battery_type=(st.section('holding').get('decoded') or {}).get('battery_type'),
                      hub_version=__version__)
        else:
            self.pub(T_EMU_AVAIL, 'offline', retain=True)
            pi = dict(self._last_pi, core_connected=False, emulator_polling=False, hub_version=__version__)
        self.pub(T_PI, pi)
        pl = st.planner
        alive = pl is not None and pl.last_tick_mono is not None and time.monotonic() - pl.last_tick_mono < 300
        self.pub(T_TOU_AVAIL, 'online' if alive else 'offline', retain=True)
        if alive and (pl.plan.get('ts') != self._last_plan_ts or time.monotonic() - self._last_tou > 60):
            d = dict(pl.plan)
            d.pop('projection', None)                  # web UI only; HA gets the hourly trace attribute
            d.update(enabled=pl.enabled, dry_run=pl.p.dry_run, last_write=pl.last_write_desc, ts=round(time.time(), 1))
            self.pub(T_TOU, d)
            self._last_plan_ts, self._last_tou = pl.plan.get('ts'), time.monotonic()

    async def run(self) -> None:
        while True:
            if self.connected:
                try:
                    self.publish_states()
                except Exception:  # noqa: BLE001
                    log.exception('MQTT publish')
            await asyncio.sleep(self.cfg.publish_s)

    def status(self) -> dict:
        return {'enabled': True, 'connected': self.connected, 'host': self.cfg.host, 'last_error': self.last_error,
                'published': self.published}
