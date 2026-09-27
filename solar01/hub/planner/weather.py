"""PV forecast: Open-Meteo global horizontal irradiance x learned yield x live correction."""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
import urllib.request
from zoneinfo import ZoneInfo

log = logging.getLogger('solar01.weather')


class Weather:
    URL = ('https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}'
           '&hourly=shortwave_radiation,cloud_cover,temperature_2m&past_days=14&forecast_days=3&timezone={tz}')

    def __init__(self, p, site):
        self.p = p
        self.site = site
        self.tz = ZoneInfo(site.tz)
        self.ghi: dict[int, float] = {}      # hour start ts -> W/m2
        self.cloud: dict[int, float] = {}
        self.temp: dict[int, float] = {}
        self.fetched = 0.0                   # wall time of the last successful fetch
        self.ok = False
        self.last_error: str | None = None
        self.k = p.pv_k_default
        self.scale = 1.0

    def fetch(self) -> bool:
        url = self.URL.format(lat=self.site.lat, lon=self.site.lon, tz=self.site.tz.replace('/', '%2F'))
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                hourly = json.load(r)['hourly']
            self.parse(hourly)
        except Exception as e:  # noqa: BLE001
            log.warning('weather fetch failed: %s', e)
            self.ok = False
            self.last_error = str(e)
            return False
        self.fetched, self.ok, self.last_error = time.time(), True, None
        return True

    def parse(self, hourly: dict) -> None:
        ghi, cloud, temp = {}, {}, {}
        temps = hourly.get('temperature_2m') or [None] * len(hourly['time'])
        for ts, g, c, tc in zip(hourly['time'], hourly['shortwave_radiation'], hourly['cloud_cover'], temps):
            t = dt.datetime.strptime(ts, '%Y-%m-%dT%H:%M').replace(tzinfo=self.tz)
            if g is not None:
                key = int(t.timestamp())
                ghi[key], cloud[key] = g, c
                if tc is not None:
                    temp[key] = tc
        if not ghi:
            raise ValueError('forecast has no irradiance values')
        self.ghi, self.cloud, self.temp = ghi, cloud, temp

    def to_cache(self) -> dict:
        return {'fetched': self.fetched, 'ghi': self.ghi, 'cloud': self.cloud, 'temp': self.temp}

    def load_cache(self, d: dict) -> None:
        """Restore the last forecast after a restart without internet (it covers 3 days)."""
        self.ghi = {int(k): v for k, v in d.get('ghi', {}).items()}
        self.cloud = {int(k): v for k, v in d.get('cloud', {}).items()}
        self.temp = {int(k): v for k, v in d.get('temp', {}).items()}
        self.fetched = float(d.get('fetched', 0))

    def ghi_at(self, t: dt.datetime) -> float:
        return self.ghi.get(int(t.timestamp() // 3600) * 3600, 0.0)

    def calibrate(self, history, now: float) -> float:
        """PV yield k = sum(actual PV) / sum(GHI) over the last 14 days of overlap."""
        sg = sp = 0.0
        for ts, _load, pv_wh, secs in history.rows(now - 14 * 86400):
            g = self.ghi.get(ts)
            if g is not None and g > 50:
                sg += g
                sp += pv_wh * 3600 / secs
        if sg > 3000:
            self.k = max(0.2, min(20.0, sp / sg))
        return self.k

    def live_scale(self, history, now: float, valid: bool = True) -> float:
        """Measured / forecast PV over the last live_pv_hours; 1.0 without enough data or in standby."""
        p = self.p
        act = fc = 0.0
        if valid:
            for ts, pv_w, secs in history.recent_pv(now - p.live_pv_hours * 3600):
                g = self.ghi.get(ts)
                if g is not None:
                    act += pv_w * secs / 3600
                    fc += g * self.k * secs / 3600
        self.scale = max(p.live_pv_min, min(p.live_pv_max, act / fc)) if fc >= p.live_pv_min_wh else 1.0
        return self.scale

    def pv_at(self, t: dt.datetime, now: dt.datetime | None = None) -> float:
        """Forecast PV W at t: GHI x yield, corrected by the live scale fading out over live_pv_fade_h."""
        w = self.ghi_at(t) * self.k
        if now is not None and self.scale != 1.0:
            f = max(0.0, 1.0 - (t - now).total_seconds() / 3600 / self.p.live_pv_fade_h)
            w *= 1.0 + (self.scale - 1.0) * f
        return w

    def day_ghi_kwh(self, d: dt.date) -> float:
        t0 = dt.datetime.combine(d, dt.time(0), self.tz)
        return sum(self.ghi_at(t0 + dt.timedelta(hours=h)) for h in range(24)) / 1000

    def hourly(self, start: dt.datetime, hours: int, now: dt.datetime | None = None) -> list[dict]:
        t0 = start.replace(minute=0, second=0, microsecond=0)
        out = []
        for h in range(hours):
            t = t0 + dt.timedelta(hours=h)
            key = int(t.timestamp())
            out.append({'ts': key, 'ghi': self.ghi.get(key), 'cloud': self.cloud.get(key),
                        'temp': self.temp.get(key), 'pv_w': round(self.pv_at(t, now))})
        return out
