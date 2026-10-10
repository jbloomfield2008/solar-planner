"""Console settings on the hub side (see solar01/settings.py).

The hub's own overrides live in the kv table ('settings') and are applied to the configuration
in place, so the planner, weather and MQTT bridge see them at once.  Core settings are sent to
the core, which checks, applies and keeps them itself; the hub reads them back from the core
state.  A change is all or nothing: the hub part is checked before the core is asked, and only
saved once the core has accepted its part.
"""
from __future__ import annotations

import asyncio

from .. import settings


class ConsoleSettings:
    def __init__(self, cfg, history, store):
        self.hist = history
        self.store = store
        self.hub = settings.Overrides(cfg, 'hub')
        self.listeners: list = []                 # called with the set of changed hub keys
        self.lock = asyncio.Lock()

    def load(self) -> None:
        skipped = self.hub.load(self.hist.get('settings'))
        if skipped:
            self.store.add_event('warning', f'console settings ignored: {", ".join(skipped)}', source='hub')

    def core_state(self) -> dict | None:
        link = self.store.core_link
        if not link or not link.connected:
            return None
        return (self.store.core_state or {}).get('settings')

    def items(self) -> list[dict]:
        return settings.items({'hub': self.hub.state(), 'core': self.core_state()})

    async def update(self, values, source: str) -> None:
        """ValueError with a user-facing reason if anything is refused; ConnectionError without the core."""
        if not isinstance(values, dict) or not values:
            raise ValueError('expected {"values": {"key": value or null}}')
        unknown = [k for k in values if k not in settings.BY_KEY]
        if unknown:
            raise ValueError(f'{unknown[0]} cannot be changed from the console')
        async with self.lock:                      # two consoles saving at once must not interleave
            await self._update(values, source)

    async def _update(self, values: dict, source: str) -> None:
        hub_part = {k: v for k, v in values.items() if settings.BY_KEY[k].owner == 'hub'}
        core_part = {k: v for k, v in values.items() if settings.BY_KEY[k].owner == 'core'}
        self.hub.update(hub_part, dry_run=True)
        if core_part:
            link = self.store.core_link
            if not link or not link.connected:
                raise ConnectionError('the core is not connected, so its settings cannot be changed')
            res = await link.request({'type': 'settings', 'values': core_part}, timeout=10)
            if not res.get('ok'):
                raise ValueError(res.get('error') or 'refused by the core')
            if self.store.core_state is not None and res.get('settings'):
                self.store.core_state['settings'] = res['settings']
        changes = self.hub.update(hub_part)
        self.hist.set('settings', self.hub.values)
        if changes:
            self.store.add_event('info', f'settings changed ({source}): {settings.describe(changes)}', source='hub')
            for fn in self.listeners:
                fn(set(changes))
