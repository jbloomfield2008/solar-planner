"""Print a solar01 config.toml equivalent to the legacy /etc/solar-mqtt.env (one-time migration).

usage: PYTHONPATH=<release> python3 deploy/migrate_env.py /etc/solar-mqtt.env > /etc/solar01/config.toml
"""
import json
import sys

from solar01 import config as C

IGNORED = {'TOU_DB', 'TOU_BOOTSTRAP', 'TOU_HOLD_HEARTBEAT_S', 'TOU_JIT_MIN_PV_KWH', 'BMS_EMU_RAW', 'BMS_EMU_PATTERN',
           'BMS_EMU_BE', 'BMS_EMU_JK_TOPIC', 'BMS_EMU_JK_FILE', 'BMS_EMU_STATE_FILE', 'SOLAR_LOCAL_DIR',
           'HOLDING_PUBLISH_SECONDS'}
FIXED = {
    'MQTT_HOST': ('hub.mqtt', 'host'), 'MQTT_PORT': ('hub.mqtt', 'port'), 'MQTT_USER': ('hub.mqtt', 'username'),
    'MQTT_PASS': ('hub.mqtt', 'password'), 'CT_CAL_MIN_WRITE_SECONDS': ('core.inverter', 'ct_cal_min_write_s'),
    'STANDBY_WATCHDOG_S': ('core.inverter', 'standby_watchdog_s'), 'PV_AVG_SECONDS': ('core.inverter', 'pv_avg_s'),
    'TOU_HOLD': ('hub.planner', 'hold_enabled'), 'TOU_ENABLED': ('hub.planner', 'enabled_default'),
    'TOU_HOLIDAYS': ('hub.planner', 'extra_holidays'), 'TOU_TZ': ('hub.site', 'tz'), 'TOU_LAT': ('hub.site', 'lat'),
    'TOU_LON': ('hub.site', 'lon'),
}


def target(key):
    if key in FIXED:
        return FIXED[key]
    if key.startswith('CT_CAL_'):
        return 'core.inverter', key.lower()
    if key.startswith('TOU_'):
        return 'hub.planner', key[4:].lower()
    if key.startswith('BMS_EMU_'):
        return 'core.emulator', key[8:].lower()
    return None


def convert(current, raw):
    if isinstance(current, bool):
        return raw.strip().lower() in ('1', 'true', 'yes', 'on')
    if isinstance(current, int):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        items = [x.strip() for x in raw.split(',') if x.strip()]
        return [int(x) for x in items] if current and isinstance(current[0], int) else items
    return raw


def main(path):
    cfg = C.Config()
    sections: dict[str, dict] = {}
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, raw = line.split('=', 1)
        key, raw = key.strip(), raw.strip().strip('"').strip("'")
        if key in IGNORED:
            continue
        t = target(key)
        obj = cfg
        try:
            for part in t[0].split('.'):
                obj = getattr(obj, part)
            current = getattr(obj, t[1])
        except (TypeError, AttributeError):
            print(f'# ignored unknown legacy setting {key}', file=sys.stderr)
            continue
        sections.setdefault(t[0], {})[t[1]] = convert(current, raw)
    if sections.get('hub.mqtt', {}).get('host'):
        sections['hub.mqtt']['enabled'] = True
    C.from_dict(_nest(sections))                 # validate
    out = [f'# generated from {path}; see deploy/config.example.toml', 'log_level = "INFO"', '']
    for name in sorted(sections):
        out.append(f'[{name}]')
        for k, v in sections[name].items():
            out.append(f'{k} = {json.dumps(v)}')
        out.append('')
    print('\n'.join(out))


def _nest(sections):
    root: dict = {}
    for name, values in sections.items():
        d = root
        for part in name.split('.'):
            d = d.setdefault(part, {})
        d.update(values)
    return root


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/etc/solar-mqtt.env')
