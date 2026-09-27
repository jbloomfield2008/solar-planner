"""Local web UI and JSON API (aiohttp).  Needs no internet and no Home Assistant.

GET  /                  the dashboard (static files from web/)
GET  /api/state         live snapshot: devices, emulator, planner, health
GET  /api/stream        server-sent events: 'state' every 2 s, 'event' as they happen
GET  /api/history       ?hours=24 -> minute averages (downsampled for long ranges)
GET  /api/daily         ?days=14 -> kWh per day (load, PV)
GET  /api/events        ?limit=100
POST /api/planner       {"enabled": true|false}
GET  /api/config        effective configuration (password redacted)
GET  /healthz           200 when the core link is up and data is fresh, else 503
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from aiohttp import web

from .. import config as config_mod

log = logging.getLogger('solar01.web')
DEFAULT_STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'web')


def _dumps(obj) -> str:
    return json.dumps(obj, default=str, separators=(',', ':'))


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps, headers={'Cache-Control': 'no-store'})


def _bucket_for(hours: float) -> int:
    if hours <= 12:
        return 60
    if hours <= 48:
        return 120
    if hours <= 24 * 7:
        return 600
    return 1800


def create_app(store, history, cfg) -> web.Application:
    static_dir = cfg.hub.web.static_dir or DEFAULT_STATIC
    app = web.Application()
    # open event streams never finish on their own; they must end as soon as shutdown starts,
    # otherwise the server waits for them and systemd kills the hub before history is saved
    shutting_down = asyncio.Event()

    async def on_shutdown(_app):
        shutting_down.set()
    app.on_shutdown.append(on_shutdown)

    async def index(_request):
        return web.FileResponse(os.path.join(static_dir, 'index.html'), headers={'Cache-Control': 'no-cache'})

    async def state(_request):
        return _json(store.public_state())

    async def stream(request):
        resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
                                           'X-Accel-Buffering': 'no'})
        await resp.prepare(request)
        q = store.subscribe()

        async def send(name, data):
            await resp.write(f'event: {name}\ndata: {_dumps(data)}\n\n'.encode())
        try:
            await send('state', store.public_state())
            next_state = time.monotonic() + 2
            while not shutting_down.is_set():
                try:
                    msg = await asyncio.wait_for(q.get(), max(0.05, next_state - time.monotonic()))
                    await send('event', msg)
                except asyncio.TimeoutError:
                    await send('state', store.public_state())
                    next_state = time.monotonic() + 2
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            store.unsubscribe(q)
        return resp

    async def history_api(request):
        try:
            hours = max(1.0, min(24 * 90.0, float(request.query.get('hours', 24))))
        except ValueError:
            return _json({'error': 'hours must be a number'}, 400)
        until = time.time()
        bucket = _bucket_for(hours)
        data = await asyncio.to_thread(history.series, until - hours * 3600, until, bucket)
        return _json({'hours': hours, 'bucket_s': bucket, **data})

    async def daily(request):
        try:
            days = max(1, min(90, int(request.query.get('days', 14))))
        except ValueError:
            return _json({'error': 'days must be an integer'}, 400)
        planner = store.planner
        cal = planner.cal if planner else None
        rows = await asyncio.to_thread(history.daily_energy, days, cal) if cal else []
        return _json({'days': rows})

    async def events(request):
        try:
            limit = max(1, min(1000, int(request.query.get('limit', 100))))
        except ValueError:
            return _json({'error': 'limit must be an integer'}, 400)
        return _json({'events': await asyncio.to_thread(history.recent_events, limit)})

    async def planner_post(request):
        if store.planner is None:
            return _json({'error': 'planner not running'}, 503)
        try:
            body = await request.json()
        except ValueError:
            return _json({'error': 'expected JSON'}, 400)
        if not isinstance(body, dict) or not isinstance(body.get('enabled'), bool):
            return _json({'error': 'expected {"enabled": true|false}'}, 400)
        await store.planner.set_enabled(body['enabled'], source=f'web {request.remote}')
        return _json(store.planner.public())

    async def config_api(_request):
        return _json(config_mod.redacted(cfg))

    async def healthz(_request):
        h = store.health()
        bad = [k for k in ('core', 'inverter', 'jk') if h.get(k, {}).get('status') == 'error']
        return _json({'ok': not bad, 'failing': bad, 'health': h}, 200 if not bad else 503)

    app.router.add_get('/', index)
    app.router.add_get('/api/state', state)
    app.router.add_get('/api/stream', stream)
    app.router.add_get('/api/history', history_api)
    app.router.add_get('/api/daily', daily)
    app.router.add_get('/api/events', events)
    app.router.add_post('/api/planner', planner_post)
    app.router.add_get('/api/config', config_api)
    app.router.add_get('/healthz', healthz)
    if os.path.isdir(static_dir):
        app.router.add_static('/static/', static_dir, append_version=False)
    return app
