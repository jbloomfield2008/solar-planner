"""Small helpers shared by both processes."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import socket
import time

# The Pi has no RTC and boots with a stale clock until NTP syncs.  Wall-clock
# timestamps before this are not trusted for history or scheduling.
MIN_VALID_TS = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp()
TIMESYNC_FLAG = '/run/systemd/timesync/synchronized'


def clock_synced() -> bool:
    if os.path.exists(TIMESYNC_FLAG):
        return True
    return time.time() > MIN_VALID_TS


def atomic_write_json(path: str, data) -> None:
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'))
    os.replace(tmp, path)


def sd_notify(message: str) -> bool:
    """Minimal sd_notify(3): READY=1, WATCHDOG=1, STATUS=... (no-op outside systemd)."""
    addr = os.environ.get('NOTIFY_SOCKET')
    if not addr or not hasattr(socket, 'AF_UNIX'):
        return False
    if addr.startswith('@'):
        addr = '\0' + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
        return True
    except OSError:
        return False


def setup_logging(level: str = 'INFO') -> None:
    # journald already timestamps every line
    fmt = '%(levelname)s %(name)s: %(message)s' if os.environ.get('JOURNAL_STREAM') \
        else '%(asctime)s %(levelname)s %(name)s: %(message)s'
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt)


def round_or_none(v, nd=1):
    return None if v is None else round(v, nd)
