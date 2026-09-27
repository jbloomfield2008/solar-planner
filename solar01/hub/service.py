"""The hub process: wires history, the core link, planner, MQTT bridge and web UI together.

Each part runs as a supervised task: if one crashes it is logged, recorded as an event and
restarted with back-off, while the others keep running.  The systemd watchdog is petted
from the event loop, so a blocked loop gets the hub restarted.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from aiohttp import web

from .. import __version__
from ..ipc import CoreClient
from ..util import clock_synced, sd_notify
from .history import History
from .planner.planner import Planner
from .state import Store
from .web import create_app

log = logging.getLogger('solar01.hub')


class Recorder:
    """Turns core state messages into history rows and feeds the planner's rate learning."""

    def __init__(self, history: History, planner: Planner):
        self.hist = history
        self.planner = planner

    def __call__(self, msg: dict) -> None:
        self.planner.observe()
        if not clock_synced():
            return
        inv = msg.get('inverter') or {}
        d, age = inv.get('data'), inv.get('age_s')
        if not d or age is None or age > 15:
            return
        jk_sec = msg.get('jk') or {}
        jk = jk_sec.get('data') if (jk_sec.get('age_s') or 1e9) < 30 else None
        now = time.time()
        pv = float(d.get('pv_power_raw', d.get('pv_power', 0)))
        soc = float(jk['soc']) if jk else float(d.get('soc', 0))
        self.hist.add_sample(now, float(d.get('load_power', 0)), pv, soc)
        temps = [t for t in ((jk or {}).get('temp_1'), (jk or {}).get('temp_2')) if t is not None]
        self.hist.add_minute(now, {
            'pv_w': pv, 'load_w': d.get('load_power'), 'batt_w': d.get('battery_power'),
            'grid_w': (d.get('grid_import_power') or 0) - (d.get('grid_export_power') or 0),
            'soc': d.get('soc'), 'jk_soc': (jk or {}).get('soc'),
            'batt_v': (jk or {}).get('voltage') or d.get('battery_voltage'),
            'cell_min': (jk or {}).get('cell_voltage_min'), 'cell_max': (jk or {}).get('cell_voltage_max'),
            'temp_c': max(temps) if temps else None, 'ct_offset_w': d.get('ct_power_offset')})


async def supervise(name: str, factory, store: Store) -> None:
    backoff = 5.0
    while True:
        started = time.monotonic()
        try:
            await factory()
            log.warning('%s task exited', name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception('%s task crashed', name)
            store.add_event('error', f'hub {name} task crashed: {e}', source='hub')
        if time.monotonic() - started > 600:
            backoff = 5.0
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300.0)


async def housekeeping(cfg, history: History, store: Store) -> None:
    last_prune = 0.0
    while True:
        await asyncio.sleep(cfg.hub.flush_s)
        await asyncio.to_thread(history.flush)
        store.persist()
        if time.monotonic() - last_prune > 86400 and clock_synced():
            await asyncio.to_thread(history.prune, time.time(), cfg.hub.minute_retention_days)
            last_prune = time.monotonic()


async def watchdog() -> None:
    while True:
        sd_notify('WATCHDOG=1')
        await asyncio.sleep(10)


def make_runner(app) -> web.AppRunner:
    try:
        return web.AppRunner(app, access_log=None, shutdown_timeout=3.0)
    except TypeError:                               # older aiohttp: timeout lives on the site
        return web.AppRunner(app, access_log=None)


async def serve_web(cfg, store: Store, history: History) -> None:
    app = create_app(store, history, cfg)
    runner = make_runner(app)
    await runner.setup()
    try:
        while True:
            try:
                site = web.TCPSite(runner, cfg.hub.web.host, cfg.hub.web.port)
                await site.start()
                log.info('web UI on http://%s:%d/', cfg.hub.web.host, cfg.hub.web.port)
                break
            except OSError as e:
                log.error('web UI cannot listen on %s:%d: %s (retrying)', cfg.hub.web.host, cfg.hub.web.port, e)
                await asyncio.sleep(30)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def amain(cfg) -> int:
    history = History(cfg.hub.db)
    imported = history.import_legacy(cfg.hub.legacy_db)
    store = Store(history, cfg)
    if imported:
        store.add_event('info', f'imported {imported} hourly rows from {cfg.hub.legacy_db}', source='hub')
    backfilled = history.backfill_minutes()
    if backfilled:
        store.add_event('info', f'added {backfilled} hourly points to the history charts', source='hub')
    core = CoreClient(cfg.core.socket, store.on_core_message, name='hub')
    store.core_link = core
    planner = Planner(cfg, history, store, core)
    store.planner = planner
    store.state_listeners.append(Recorder(history, planner))
    loop = asyncio.get_running_loop()
    mqtt = None
    if cfg.hub.mqtt.enabled and cfg.hub.mqtt.host:
        from .mqtt import MqttBridge
        mqtt = MqttBridge(cfg, store, loop)
        store.mqtt = mqtt
        mqtt.start()

    async def planner_task():
        for _ in range(45):                           # wait for the core's first inverter poll
            if ((store.core_state or {}).get('inverter') or {}).get('data'):
                break
            await asyncio.sleep(1)
        await planner.run()

    tasks = [asyncio.create_task(supervise('core-link', core.run, store)),
             asyncio.create_task(supervise('planner', planner_task, store)),
             asyncio.create_task(supervise('housekeeping', lambda: housekeeping(cfg, history, store), store)),
             asyncio.create_task(watchdog())]
    if cfg.hub.web.enabled:
        tasks.append(asyncio.create_task(supervise('web', lambda: serve_web(cfg, store, history), store)))
    if mqtt:
        tasks.append(asyncio.create_task(supervise('mqtt', mqtt.run, store)))
    store.add_event('info', f'hub {__version__} started', source='hub')
    sd_notify('READY=1')
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        # save first, so a slow part of the shutdown can never cost history or events
        log.info('shutting down')
        store.persist()
        history.flush()
        for t in tasks:
            t.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 8)
        except asyncio.TimeoutError:
            log.warning('shutdown: tasks did not stop within 8 s')
        if mqtt:
            mqtt.stop()
        store.persist()
        history.close()
        log.info('stopped')
    return 0


def main(cfg) -> int:
    try:
        return asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        return 0
