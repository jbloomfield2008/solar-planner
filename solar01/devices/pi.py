"""Raspberry Pi health: undervoltage/throttling flags, temperature, load, memory, disk, uptime."""
from __future__ import annotations

import os
import shutil
import subprocess


def health() -> dict:
    h: dict = {}
    try:
        out = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=5).stdout
        bits = int(out.split('=')[1], 16)
        h.update(throttled_raw=f'{bits:#x}', undervoltage_now=bool(bits & 0x1), throttled_now=bool(bits & 0x4),
                 undervoltage_since_boot=bool(bits & 0x10000), throttled_since_boot=bool(bits & 0x40000))
    except Exception:  # noqa: BLE001
        pass
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            h['cpu_temp'] = round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        pass
    try:
        h['load1'] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    h['mem_available_mb'] = round(int(line.split()[1]) / 1024)
                    break
        with open('/proc/uptime') as f:
            h['uptime_s'] = round(float(f.read().split()[0]))
    except (OSError, ValueError):
        pass
    try:
        h['disk_free_mb'] = round(shutil.disk_usage('/').free / 2**20)
    except OSError:
        pass
    return h
