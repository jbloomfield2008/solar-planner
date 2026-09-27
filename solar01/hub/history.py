"""SQLite history: hourly energy (planner learning), minute averages (charts), events, settings.

Writes go to the connection as they happen and are committed every flush_s (default
5 min), so the SD card sees one small transaction per flush.  A crash loses at most
that much history.  WAL mode keeps chart reads from blocking writes.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading

MINUTE_FIELDS = ('pv_w', 'load_w', 'batt_w', 'grid_w', 'soc', 'jk_soc', 'batt_v', 'cell_min', 'cell_max', 'temp_c',
                 'ct_offset_w')

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS hourly (ts INTEGER PRIMARY KEY, load_wh REAL, pv_wh REAL, secs REAL, soc REAL);
CREATE TABLE IF NOT EXISTS minute (ts INTEGER PRIMARY KEY, {', '.join(f + ' REAL' for f in MINUTE_FIELDS)});
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, source TEXT,
                                   msg TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class History:
    def __init__(self, path: str):
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        if path != ':memory:':
            self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute('PRAGMA table_info(minute)')}
        for field in MINUTE_FIELDS:                  # columns added in later versions
            if field not in have:
                self.db.execute(f'ALTER TABLE minute ADD COLUMN {field} REAL')
        self.db.commit()
        self.lock = threading.RLock()
        self.bucket = None                  # [hour ts, load_wh, pv_wh, secs, soc]
        self.last_sample = None
        self.minute = None                  # [minute ts, {field: [sum(v*dt), sum(dt)]}]
        self.last_minute_sample = None

    # -- settings ------------------------------------------------------------------------------
    def get(self, k: str, default=None):
        with self.lock:
            r = self.db.execute('SELECT v FROM kv WHERE k=?', (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k: str, v) -> None:
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (k, json.dumps(v)))
            self.db.commit()

    # -- hourly energy (planner) -------------------------------------------------------------------
    def add_sample(self, now: float, load_w: float, pv_w: float, soc: float) -> None:
        hour = int(now // 3600) * 3600
        if self.last_sample is None:
            self.last_sample = now
            return
        dt_s = min(now - self.last_sample, 60.0)
        self.last_sample = now
        if dt_s <= 0:
            return
        with self.lock:
            if self.bucket is None or self.bucket[0] != hour:
                self._write_bucket()
                row = self.db.execute('SELECT load_wh, pv_wh, secs FROM hourly WHERE ts=?', (hour,)).fetchone()
                self.bucket = [hour, row[0], row[1], row[2], soc] if row and row[2] < 3600 else [hour, 0.0, 0.0, 0.0, soc]
            b = self.bucket
            b[1] += load_w * dt_s / 3600
            b[2] += pv_w * dt_s / 3600
            b[3] += dt_s
            b[4] = soc

    def _write_bucket(self) -> None:
        if self.bucket:
            self.db.execute('INSERT OR REPLACE INTO hourly VALUES (?,?,?,?,?)', tuple(self.bucket))

    def rows(self, since_ts: float):
        with self.lock:
            return self.db.execute('SELECT ts, load_wh, pv_wh, secs FROM hourly WHERE ts>=? AND secs>=1800 ORDER BY ts',
                                   (since_ts,)).fetchall()

    def recent_pv(self, since_ts: float):
        """[(hour ts, mean PV W, secs)] since since_ts, including the partial current hour."""
        with self.lock:
            rows = self.db.execute('SELECT ts, pv_wh, secs FROM hourly WHERE ts>=? AND secs>=600 ORDER BY ts',
                                   (since_ts,)).fetchall()
            out = [(ts, pv_wh * 3600 / secs, secs) for ts, pv_wh, secs in rows]
            b = self.bucket
            if b and b[3] >= 600 and b[0] >= since_ts and all(r[0] != b[0] for r in out):
                out.append((b[0], b[2] * 3600 / b[3], b[3]))
        return out

    def load_profile(self, now: float, cal, history_days: int, prior_days: float) -> dict:
        """{(daytype, hour): mean W}.  A day-type mean seen on few days is shrunk toward the
        all-days mean for that hour (prior_days pseudo-days)."""
        prof, allh, total = {}, {}, []
        for ts, load_wh, _pv, secs in self.rows(now - history_days * 86400):
            t = dt.datetime.fromtimestamp(ts, cal.tz)
            w = load_wh * 3600 / secs
            prof.setdefault(('we' if cal.is_offpeak_day(t.date()) else 'wd', t.hour), []).append(w)
            allh.setdefault(t.hour, []).append(w)
            total.append(w)
        byhour = {k: sum(v) / len(v) for k, v in allh.items()}
        bytype = {k: (sum(v) + prior_days * byhour[k[1]]) / (len(v) + prior_days) for k, v in prof.items()}
        return {'bytype': bytype, 'byhour': byhour, 'global': sum(total) / len(total) if total else 600.0,
                'days': len(total) / 24}

    # -- minute averages (charts) -------------------------------------------------------------------
    def add_minute(self, now: float, values: dict) -> None:
        m = int(now // 60) * 60
        with self.lock:
            if self.last_minute_sample is None:
                self.last_minute_sample = now
                self.minute = [m, {}]
                return
            dt_s = min(now - self.last_minute_sample, 30.0)
            self.last_minute_sample = now
            if dt_s <= 0:
                return
            if self.minute[0] != m:
                self._write_minute()
                self.minute = [m, {}]
            sums = self.minute[1]
            for k in MINUTE_FIELDS:
                v = values.get(k)
                if v is not None:
                    s = sums.setdefault(k, [0.0, 0.0])
                    s[0] += float(v) * dt_s
                    s[1] += dt_s

    def _write_minute(self) -> None:
        if not self.minute or not self.minute[1]:
            return
        m, sums = self.minute
        row = [m] + [sums[k][0] / sums[k][1] if k in sums and sums[k][1] > 0 else None for k in MINUTE_FIELDS]
        self.db.execute(f'INSERT OR REPLACE INTO minute (ts, {", ".join(MINUTE_FIELDS)}) VALUES ({",".join("?" * len(row))})',
                        row)

    def series(self, since_ts: float, until_ts: float, bucket_s: int = 60) -> dict:
        bucket_s = max(60, int(bucket_s))
        cols = ', '.join(f'AVG({f})' for f in MINUTE_FIELDS)
        with self.lock:
            rows = self.db.execute(f'SELECT (ts / ?) * ? AS t, {cols} FROM minute WHERE ts >= ? AND ts < ? '
                                   f'GROUP BY t ORDER BY t', (bucket_s, bucket_s, int(since_ts), int(until_ts))).fetchall()
        out = {'t': [r[0] for r in rows]}
        for i, f in enumerate(MINUTE_FIELDS, start=1):
            nd = 1 if f in ('soc', 'jk_soc', 'temp_c', 'ct_offset_w') else (2 if f == 'batt_v' else 0)
            out[f] = [None if r[i] is None else round(r[i], nd) for r in rows]
        return out

    def daily_energy(self, days: int, cal) -> list[dict]:
        """Per local day kWh of load and PV from the hourly table."""
        since = dt.datetime.now(cal.tz).replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=days - 1)
        with self.lock:
            rows = self.db.execute('SELECT ts, load_wh, pv_wh FROM hourly WHERE ts >= ? ORDER BY ts',
                                   (int(since.timestamp()),)).fetchall()
            b = self.bucket
        acc: dict[str, list[float]] = {}
        seen = set()
        for ts, load_wh, pv_wh in rows:
            if b and ts == b[0]:
                continue
            d = dt.datetime.fromtimestamp(ts, cal.tz).date().isoformat()
            a = acc.setdefault(d, [0.0, 0.0])
            a[0] += load_wh or 0
            a[1] += pv_wh or 0
            seen.add(ts)
        if b and b[0] >= since.timestamp():
            d = dt.datetime.fromtimestamp(b[0], cal.tz).date().isoformat()
            a = acc.setdefault(d, [0.0, 0.0])
            a[0] += b[1]
            a[1] += b[2]
        return [{'date': d, 'load_kwh': round(v[0] / 1000, 2), 'pv_kwh': round(v[1] / 1000, 2)} for d, v in sorted(acc.items())]

    # -- events ----------------------------------------------------------------------------------
    def add_event(self, ts: float, level: str, source: str, msg: str, data: dict | None = None) -> None:
        with self.lock:
            self.db.execute('INSERT INTO events (ts, level, source, msg, data) VALUES (?,?,?,?,?)',
                            (ts, level, source, msg, json.dumps(data) if data else None))

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute('SELECT id, ts, level, source, msg, data FROM events ORDER BY id DESC LIMIT ?',
                                   (int(limit),)).fetchall()
        return [{'id': r[0], 'ts': r[1], 'level': r[2], 'source': r[3], 'msg': r[4],
                 'data': json.loads(r[5]) if r[5] else None} for r in rows]

    # -- maintenance -------------------------------------------------------------------------------
    def flush(self) -> None:
        with self.lock:
            self._write_bucket()
            self._write_minute()
            self.db.commit()

    def prune(self, now: float, minute_retention_days: int, event_retention_days: int = 365) -> None:
        with self.lock:
            self.db.execute('DELETE FROM minute WHERE ts < ?', (now - minute_retention_days * 86400,))
            self.db.execute('DELETE FROM events WHERE ts < ?', (now - event_retention_days * 86400,))
            self.db.commit()

    def import_legacy(self, path: str) -> int:
        """Copy solar_tou.py's hourly history and settings into an empty database."""
        if not path or not os.path.exists(path):
            return 0
        with self.lock:
            if self.db.execute('SELECT count(*) FROM hourly').fetchone()[0]:
                return 0
            src = sqlite3.connect(path)
            try:
                rows = src.execute('SELECT ts, load_wh, pv_wh, secs, soc FROM hourly').fetchall()
                kv = src.execute('SELECT k, v FROM kv').fetchall()
            finally:
                src.close()
            self.db.executemany('INSERT OR REPLACE INTO hourly VALUES (?,?,?,?,?)', rows)
            for k, v in kv:
                if k in ('enabled', 'ac_chg_w'):
                    self.db.execute('INSERT OR IGNORE INTO kv VALUES (?,?)', (k, v))
            self.db.commit()
        return len(rows)

    def backfill_minutes(self) -> int:
        """One chart row per hour (at mid-hour) for hourly history older than the first minute row,
        so the multi-day charts are not empty after an import.  Runs once."""
        with self.lock:
            if self.get('minute_backfill_done'):
                return 0
            first = self.db.execute('SELECT min(ts) FROM minute').fetchone()[0]
            cutoff = first if first is not None else 1e18
            rows = self.db.execute('SELECT ts, load_wh, pv_wh, secs, soc FROM hourly WHERE ts + 3600 <= ? AND secs >= 600',
                                   (cutoff,)).fetchall()
            for ts, load_wh, pv_wh, secs, soc in rows:
                # the legacy hourly SOC is the inverter's reading (equal to the BMS only in closed loop)
                vals = {'pv_w': pv_wh * 3600 / secs, 'load_w': load_wh * 3600 / secs, 'soc': soc}
                row = [ts + 1800] + [vals.get(f) for f in MINUTE_FIELDS]
                self.db.execute(f'INSERT OR IGNORE INTO minute (ts, {", ".join(MINUTE_FIELDS)}) '
                                f'VALUES ({",".join("?" * len(row))})', row)
            self.db.execute("INSERT OR REPLACE INTO kv VALUES ('minute_backfill_done', 'true')")
            self.db.commit()
        return len(rows)

    def close(self) -> None:
        self.flush()
        self.db.close()
