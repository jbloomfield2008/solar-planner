"""Live state shared by the hub's parts: latest core snapshot, events, component health."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from .. import __version__
from ..util import clock_synced

log = logging.getLogger('solar01.hub')


def _status(status: str, msg: str = '') -> dict:
    return {'status': status, 'msg': msg}


class Store:
    def __init__(self, history, cfg):
        self.hist = history
        self.cfg = cfg
        self.core_state: dict | None = None
        self.core_rx_mono: float | None = None
        self.core_link = None
        self.planner = None
        self.mqtt = None
        self.state_listeners: list = []
        self.subscribers: set[asyncio.Queue] = set()
        self.last_core_event_ts = float(history.get('last_core_event_ts', 0.0))
        self._recorded_before_start = self.last_core_event_ts   # core replays its backlog on every connect
        self._recent_event_keys: deque = deque(maxlen=400)
        self.started_mono = time.monotonic()

    def link_age(self) -> float:
        return 0.0 if self.core_rx_mono is None else time.monotonic() - self.core_rx_mono

    # -- core messages -------------------------------------------------------------------------------
    def on_core_message(self, msg: dict) -> None:
        kind = msg.get('type')
        if kind == 'state':
            self.core_state = msg
            self.core_rx_mono = time.monotonic()
            for fn in self.state_listeners:
                try:
                    fn(msg)
                except Exception:  # noqa: BLE001
                    log.exception('state listener')
        elif kind == 'event':
            self._core_event(msg)
        elif kind == 'events':
            for e in msg.get('events') or []:
                self._core_event(e)

    def _core_event(self, e: dict) -> None:
        ts = float(e.get('ts') or 0)
        key = (ts, e.get('msg'))
        if ts <= self._recorded_before_start or ts < self.last_core_event_ts or key in self._recent_event_keys:
            return
        self._recent_event_keys.append(key)
        self.last_core_event_ts = max(self.last_core_event_ts, ts)
        data = {k: v for k, v in e.items() if k not in ('type', 'ts', 'level', 'source', 'msg')}
        self._record(ts, e.get('level', 'info'), e.get('source', 'core'), e.get('msg', ''), data or None)

    def add_event(self, level: str, msg: str, source: str = 'hub', **data) -> None:
        log.log(logging.WARNING if level in ('warning', 'error') else logging.INFO, '%s', msg)
        self._record(time.time(), level, source, msg, data or None)

    def _record(self, ts, level, source, msg, data) -> None:
        if clock_synced():
            self.hist.add_event(ts, level, source, msg, data)
        self.notify({'type': 'event', 'ts': ts, 'level': level, 'source': source, 'msg': msg})

    def persist(self) -> None:
        self.hist.set('last_core_event_ts', self.last_core_event_ts)

    # -- server-sent events ---------------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def notify(self, msg: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # -- views ----------------------------------------------------------------------------------------
    def section(self, name: str) -> dict:
        d = dict((self.core_state or {}).get(name) or {})
        if d.get('age_s') is not None:
            d['age_s'] = round(d['age_s'] + self.link_age(), 1)
        return d

    def health(self) -> dict:
        h: dict[str, dict] = {}
        link = self.core_link
        s = self.core_state
        la = self.link_age()
        if not link or not link.connected:
            h['core'] = _status('error', 'hub is not connected to the core process')
        elif s is None or la > 10:
            h['core'] = _status('error', f'no state from core for {la:.0f} s')
        else:
            h['core'] = _status('ok', f"core {s.get('version')} up {s.get('uptime_s', 0) // 60} min")
        inv, jk = self.section('inverter'), self.section('jk')
        for name, sec, label in (('inverter', inv, 'inverter'), ('jk', jk, 'JK BMS')):
            age = sec.get('age_s')
            if s is None:
                h[name] = _status('off', 'no data')
            elif age is None:
                h[name] = _status('error', f"{label}: no data yet ({sec.get('last_error') or 'waiting'})")
            elif age > 60:
                h[name] = _status('error', f"{label}: last data {age:.0f} s ago ({sec.get('last_error')})")
            elif age > 20:
                h[name] = _status('warn', f'{label}: data {age:.0f} s old')
            else:
                h[name] = _status('ok', f'{label}: data {age:.0f} s old')
        emu = (s or {}).get('emulator') or {}
        decoded = self.section('holding').get('decoded') or {}
        if s is None:
            h['emulator'] = _status('off', 'no data')
        elif not emu.get('answering'):
            h['emulator'] = _status('error', f"not answering: JK data {emu.get('limit_reason')}")
        elif not emu.get('polling'):
            mode = decoded.get('battery_type')
            h['emulator'] = _status('warn', 'inverter is not polling the BMS port'
                                    + (f' (battery type {mode})' if mode and mode != 'lithium' else ''))
        elif not emu.get('charge_ok'):
            h['emulator'] = _status('warn', f"charge forbidden: {emu.get('limit_reason')}")
        else:
            h['emulator'] = _status('ok', f"closed loop, {emu.get('max_charge_a')} A charge limit")
        ct = (s or {}).get('ct') or {}
        if not ct:
            h['ct'] = _status('off', 'no data')
        elif not ct.get('enabled'):
            h['ct'] = _status('off', f"calibration off, offset {ct.get('offset_w')} W")
        elif ct.get('last_error'):
            h['ct'] = _status('warn', f"last offset write failed: {ct['last_error']}")
        else:
            h['ct'] = _status('ok', f"offset {ct.get('offset_w')} W for {ct.get('load_w'):.0f} W load"
                              if ct.get('load_w') is not None else f"offset {ct.get('offset_w')} W")
        pl = self.planner
        if pl is None:
            h['planner'] = _status('off', 'not running')
        elif pl.last_error:
            h['planner'] = _status('error', pl.last_error)
        elif not pl.enabled:
            h['planner'] = _status('warn', 'disabled')
        elif pl.plan.get('action') in ('stale', 'waiting', 'starting'):
            h['planner'] = _status('warn', pl.plan.get('reason', 'not planning'))
        elif pl.plan.get('warning'):
            h['planner'] = _status('warn', pl.plan['warning'])
        else:
            h['planner'] = _status('ok', pl.plan.get('reason', ''))
        if pl is not None:
            fetched = pl.weather.fetched
            if not fetched:
                h['weather'] = _status('error', pl.weather.last_error or 'no forecast yet')
            elif time.time() - fetched > 3 * 3600:
                h['weather'] = _status('warn', f'forecast {(time.time() - fetched) / 3600:.0f} h old '
                                               f'({pl.weather.last_error or "refresh pending"})')
            else:
                h['weather'] = _status('ok', f'forecast {(time.time() - fetched) / 60:.0f} min old')
        m = self.mqtt
        if m is None:
            h['mqtt'] = _status('off', 'Home Assistant bridge disabled')
        elif m.connected:
            h['mqtt'] = _status('ok', f'connected to {m.cfg.host}')
        else:
            h['mqtt'] = _status('warn', f'not connected to {m.cfg.host}' + (f' ({m.last_error})' if m.last_error else ''))
        h['clock'] = _status('ok', 'synchronised') if clock_synced() else _status('warn', 'waiting for NTP')
        pi = (s or {}).get('pi') or {}
        if pi.get('undervoltage_now') or pi.get('throttled_now'):
            h['pi'] = _status('warn', f"undervoltage/throttled now ({pi.get('throttled_raw')})")
        elif pi:
            temp = f"CPU {pi['cpu_temp']} C" if pi.get('cpu_temp') is not None else 'running'
            h['pi'] = _status('ok', temp + (', undervoltage seen since boot' if pi.get('undervoltage_since_boot') else ''))
        else:
            h['pi'] = _status('off', 'no data')
        return h

    def public_state(self) -> dict:
        s = self.core_state or {}
        emu = dict(s.get('emulator') or {})
        if emu.get('poll_age_s') is not None:
            emu['poll_age_s'] = round(emu['poll_age_s'] + self.link_age(), 1)
        return {
            'ts': round(time.time(), 1), 'tz': self.cfg.hub.site.tz, 'version': __version__,
            'core_version': s.get('version'),
            'simulate': bool(s.get('simulate')), 'core_uptime_s': s.get('uptime_s'),
            'hub_uptime_s': round(time.monotonic() - self.started_mono),
            'core_connected': bool(self.core_link and self.core_link.connected),
            'inverter': self.section('inverter'), 'holding': self.section('holding'), 'jk': self.section('jk'),
            'emulator': emu, 'ct': s.get('ct') or {}, 'pi': s.get('pi') or {}, 'safety': s.get('safety') or {},
            'workers': s.get('workers') or {},
            'planner': self.planner.public() if self.planner else None,
            'mqtt': self.mqtt.status() if self.mqtt else {'enabled': False},
            'health': self.health(),
        }
