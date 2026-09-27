"""The core process.

Threads, each supervised by the main loop:

* ``jk``        - polls the JK BMS every 5 s.
* ``emulator``  - answers the inverter's battery-port polls from the latest JK sample
                  (Luxpower protocol).  Needs nothing but the JK thread.
* ``inverter``  - polls the meter port every 5 s, refreshes control registers, runs the
                  CT calibration, performs hub write requests under the safety rules
                  and runs the standby watchdog.
* ``pi``        - Pi health every 30 s (not critical).

The main loop broadcasts a state snapshot to connected hubs every 2 s, writes it to
/run/solar01/core-state.json, and pets the systemd watchdog only while the critical
threads are alive and making progress.  Sample ages use time.monotonic() because the
Pi's wall clock steps at boot.
"""
from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from collections import deque

from .. import __version__
from ..config import Config
from ..devices import bmsemu, flexboss, jkbms, modbus, pi
from ..devices.flexboss import BIT_NORMAL, H_CT_OFFSET, H_FUNC
from ..ipc import IpcServer
from ..util import atomic_write_json, sd_notify
from . import safety
from .ct import CtCalibrator

log = logging.getLogger('solar01.core')
INVERTER_POLL_FRAME = bytes.fromhex('010300001000480a')
NOT_POLLED_AFTER_S = 30.0


class DeviceStats:
    def __init__(self):
        self.ok = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_error_ts: float | None = None
        self._last_log = -1e9

    def success(self) -> None:
        self.ok += 1

    def failure(self, err, what: str) -> None:
        self.errors += 1
        self.last_error = str(err)
        self.last_error_ts = round(time.time(), 1)
        if time.monotonic() - self._last_log > 60:
            log.warning('%s: %s', what, err)
            self._last_log = time.monotonic()

    def as_dict(self) -> dict:
        return {'ok': self.ok, 'errors': self.errors, 'last_error': self.last_error,
                'last_error_ts': self.last_error_ts}


class Worker(threading.Thread):
    critical = True
    max_age_s = 60.0

    def __init__(self, core: 'Core', name: str):
        super().__init__(name=name, daemon=True)
        self.core = core
        self._beat = time.monotonic()

    def beat(self) -> None:
        self._beat = time.monotonic()

    def age(self) -> float:
        return time.monotonic() - self._beat

    def run(self) -> None:
        try:
            self.loop()
        except Exception:  # noqa: BLE001 - loop() handles expected errors; this is a bug
            log.exception('%s worker crashed', self.name)


class JkWorker(Worker):
    def loop(self) -> None:
        core, cfg, bms = self.core, self.core.c.jkbms, self.core.devices['jk']
        stats = core.stats['jk']
        while not core.stop.is_set():
            t0 = time.monotonic()
            self.beat()
            try:
                sample = bms.poll()
                if sample:
                    core.set_jk(sample)
                    stats.success()
                else:
                    stats.failure('no or implausible reply', 'JK BMS')
            except OSError as e:
                bms.close()
                stats.failure(e, 'JK BMS serial')
                core.stop.wait(2)
            except Exception as e:  # noqa: BLE001
                log.exception('JK worker')
                stats.failure(e, 'JK BMS')
                core.stop.wait(2)
            core.stop.wait(max(0.1, cfg.poll_s - (time.monotonic() - t0)))


class EmulatorWorker(Worker):
    max_age_s = 30.0

    def loop(self) -> None:
        core, cfg = self.core, self.core.c.emulator
        if core.c.simulate:                      # no battery port: play the inverter's 500 ms poll
            while not core.stop.is_set():
                self.beat()
                core.slave.handle(INVERTER_POLL_FRAME)
                core.stop.wait(0.5)
            return
        factory = core.devices['emulator_serial']
        ser = None
        log.info('BMS emulator serving addr(s) %s on %s @ %d', cfg.addrs, cfg.port, cfg.baud)
        while not core.stop.is_set():
            self.beat()
            try:
                if ser is None:
                    ser = factory(cfg.port, cfg.baud, 0.02)
                frame = bmsemu.LuxSlave.read_frame(ser)
                if frame:
                    out = core.slave.handle(frame)
                    if out:
                        ser.write(out)
            except OSError as e:
                core.stats['emulator'].failure(e, 'BMS emulator serial')
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:  # noqa: BLE001
                        pass
                ser = None
                core.stop.wait(2)
            except Exception as e:  # noqa: BLE001
                log.exception('emulator worker')
                core.stats['emulator'].failure(e, 'BMS emulator')
                core.stop.wait(0.5)


class InverterWorker(Worker):
    max_age_s = 120.0

    def loop(self) -> None:
        core, cfg, inv = self.core, self.core.c.inverter, self.core.devices['inverter']
        stats = core.stats['inverter']
        pv_window: deque = deque()
        next_poll = next_holding = 0.0
        while not core.stop.is_set():
            self.beat()
            try:
                if not core.commands.empty():
                    self.process_commands(inv)
                    next_holding = 0.0
                now = time.monotonic()
                if now >= next_poll:
                    next_poll = now + cfg.poll_s
                    data = inv.read_inputs()
                    stats.success()
                    pv_window.append((now, data['pv_power']))
                    while now - pv_window[0][0] > cfg.pv_avg_s:
                        pv_window.popleft()
                    data['pv_power_raw'] = data['pv_power']
                    data['pv_power'] = round(sum(v for _, v in pv_window) / len(pv_window))
                    if now >= next_holding:
                        try:
                            snap = inv.read_holding_snapshot()
                            core.set_holding(snap)
                            core.ct.observe_register(snap.get(H_CT_OFFSET))
                            core.stats['holding'].success()
                            next_holding = now + cfg.holding_refresh_s
                        except (modbus.LinkError, modbus.ModbusException) as e:
                            core.stats['holding'].failure(e, 'inverter holding registers')
                    ct = core.ct
                    result = ct.step(data['load_power'], lambda v: inv.write_holding(H_CT_OFFSET, v), now)
                    if result == 'written':
                        core.holding_update(H_CT_OFFSET, ct.current_reg)
                    elif result == 'failed':
                        core.stats['holding'].failure(ct.last_error, 'CT offset write')
                    data['ct_power_offset'] = None if ct.current_reg is None else ct.current_reg / flexboss.CT_REG_PER_W
                    data['ct_power_offset_target'] = None if ct.target_reg is None else ct.target_reg / flexboss.CT_REG_PER_W
                    core.set_inverter(data)
                    self.standby_watchdog(inv)
            except OSError as e:
                inv.close()
                stats.failure(e, 'inverter serial')
                core.stop.wait(2)
            except (modbus.LinkError, modbus.ModbusException) as e:
                stats.failure(e, 'inverter link')
            except Exception as e:  # noqa: BLE001
                log.exception('inverter worker')
                stats.failure(e, 'inverter')
                core.stop.wait(2)
            core.wake.wait(max(0.05, next_poll - time.monotonic()))
            core.wake.clear()

    def process_commands(self, inv) -> None:
        core = self.core
        while True:
            try:
                client, msg = core.commands.get_nowait()
            except queue.Empty:
                return
            desc = str(msg.get('desc') or '')[:200]
            results: list[dict] = []
            ok = True
            for item in msg.get('writes') or []:
                try:
                    reg, value = int(item[0]), int(item[1])
                except (TypeError, ValueError, IndexError):
                    results.append({'ok': False, 'error': f'malformed write {item!r}'})
                    ok = False
                    break
                res = {'reg': reg, 'value': value}
                try:
                    current = inv.read_holding(reg) & 0xFFFF if reg in safety.NEEDS_CURRENT else None
                    err = safety.check_write(reg, value, current, core.c.inverter.qc_max_arm_min)
                    if err:
                        res.update(ok=False, error=err)
                    else:
                        inv.write_holding(reg, value)
                        readback = inv.read_holding(reg) & 0xFFFF
                        res.update(ok=True, readback=readback)
                        core.holding_update(reg, readback)
                        if reg == H_FUNC and not readback & BIT_NORMAL:
                            core.last_standby_assert = time.monotonic()
                except modbus.ModbusException as e:
                    res.update(ok=False, error=str(e))
                except modbus.LinkError as e:
                    res.update(ok=False, error=f'link: {e}')
                except OSError as e:
                    inv.close()
                    res.update(ok=False, error=f'serial: {e}')
                results.append(res)
                if not res['ok']:
                    ok = False
                    break
            ok = ok and bool(results)
            client.send_msg({'type': 'result', 'id': msg.get('id'), 'ok': ok, 'results': results})
            summary = ', '.join(f"H{r.get('reg')}={r.get('value')}" for r in results)
            core.event('info' if ok else 'warning',
                       f"write {desc or summary}: {'ok' if ok else results[-1].get('error', 'failed') if results else 'empty'}",
                       source=client.name, results=results)

    def standby_watchdog(self, inv) -> None:
        core = self.core
        since = time.monotonic() - core.last_standby_assert
        if not safety.standby_watchdog_due(core.holding.get(H_FUNC), since, core.c.inverter):
            return
        core.last_standby_assert = time.monotonic()          # one attempt per watchdog period
        cur = inv.read_holding(H_FUNC) & 0xFFFF
        if not cur & BIT_NORMAL:
            inv.write_holding(H_FUNC, cur | BIT_NORMAL)
            cur = inv.read_holding(H_FUNC) & 0xFFFF
            core.event('warning', f'standby watchdog: no standby hold asserted for {since / 60:.0f} min, '
                                  f'inverter set back to normal ({"ok" if cur & BIT_NORMAL else "FAILED"})')
        core.holding_update(H_FUNC, cur)


class PiWorker(Worker):
    critical = False
    max_age_s = 180.0

    def loop(self) -> None:
        core = self.core
        while not core.stop.is_set():
            self.beat()
            try:
                core.pi = pi.health()
            except Exception:  # noqa: BLE001
                log.exception('pi health')
            core.stop.wait(core.c.pi_health_s)


def _real_devices(c) -> dict:
    import serial

    def open_serial(port, baud, timeout):
        return serial.Serial(port, baud, timeout=timeout)

    master = modbus.RtuMaster(c.inverter.port, c.inverter.baud, c.inverter.slave, serial_factory=open_serial)
    return {'inverter': _ClosableFlexBoss(master),
            'jk': jkbms.JkBms(c.jkbms.port, c.jkbms.baud, c.jkbms.cells, serial_factory=open_serial),
            'emulator_serial': open_serial}


class _ClosableFlexBoss(flexboss.FlexBoss):
    def close(self) -> None:
        self.master.close()


def _sim_devices() -> dict:
    from ..devices import sim
    plant = sim.SimPlant()
    return {'inverter': sim.SimInverter(plant), 'jk': sim.SimJk(plant), 'emulator_serial': None}


class Core:
    def __init__(self, cfg: Config, devices: dict | None = None):
        self.cfg = cfg
        self.c = cfg.core
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.emu_lock = threading.Lock()
        self.started_mono = time.monotonic()
        self.jk: dict | None = None
        self.jk_mono: float | None = None
        self.inverter: dict | None = None
        self.inverter_mono: float | None = None
        self.holding: dict[int, int] = {}
        self.holding_mono: float | None = None
        self.pi: dict = {}
        self.stats = {k: DeviceStats() for k in ('inverter', 'holding', 'jk', 'emulator')}
        self.events: deque = deque(maxlen=200)
        self.commands: queue.Queue = queue.Queue()
        self.last_standby_assert = time.monotonic()
        self.limiter = bmsemu.ChargeLimiter(self.c.emulator)
        self.ct = CtCalibrator(self.c.inverter)
        self.slave = bmsemu.LuxSlave(self.c.emulator, self.emulator_registers)
        self.devices = devices or (_sim_devices() if self.c.simulate else _real_devices(self.c))
        self.server = IpcServer(self.c.socket, self.on_message, self.on_connect)
        self.workers = [JkWorker(self, 'jk'), EmulatorWorker(self, 'emulator'), InverterWorker(self, 'inverter'),
                        PiWorker(self, 'pi')]
        self._polling: bool | None = None
        self._ct_error: str | None = None
        self._last_reason: str | None = None

    # -- shared state ------------------------------------------------------------------------
    def set_jk(self, sample: dict) -> None:
        with self.lock:
            self.jk, self.jk_mono = sample, time.monotonic()

    def set_inverter(self, data: dict) -> None:
        with self.lock:
            self.inverter, self.inverter_mono = data, time.monotonic()

    def set_holding(self, snap: dict[int, int]) -> None:
        with self.lock:
            self.holding, self.holding_mono = dict(snap), time.monotonic()

    def holding_update(self, reg: int, value: int) -> None:
        with self.lock:
            h = dict(self.holding)
            h[reg] = flexboss.signed16(value) if reg in flexboss.SIGNED_HOLDING else value
            self.holding = h

    def emulator_registers(self):
        with self.lock:
            jk, mono = self.jk, self.jk_mono
        age = time.monotonic() - mono if mono is not None else float('inf')
        with self.emu_lock:
            return bmsemu.build_registers(jk, age, self.c.emulator, self.limiter)

    # -- events & IPC ----------------------------------------------------------------------------
    def event(self, level: str, msg: str, **data) -> None:
        e = {'type': 'event', 'ts': round(time.time(), 1), 'level': level, 'source': data.pop('source', 'core'),
             'msg': msg, **data}
        self.events.append(e)
        log.log(logging.WARNING if level in ('warning', 'error') else logging.INFO, '%s', msg)
        self.server.broadcast(e)

    def on_connect(self, client) -> None:
        client.send_msg({'type': 'events', 'events': list(self.events)})
        client.send_msg(self.snapshot())

    def on_message(self, client, msg: dict) -> None:
        kind = msg.get('type')
        if kind == 'hello':
            client.name = str(msg.get('name') or '?')[:32]
            log.info('ipc client connected: %s', client.name)
        elif kind == 'write':
            self.commands.put((client, msg))
            self.wake.set()
        elif kind == 'assert_standby':
            self.last_standby_assert = time.monotonic()
        elif kind == 'ping':
            client.send_msg({'type': 'result', 'id': msg.get('id'), 'ok': True, 'pong': True})
        else:
            client.send_msg({'type': 'result', 'id': msg.get('id'), 'ok': False, 'error': f'unknown type {kind!r}'})

    # -- snapshot ---------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        now = time.monotonic()
        with self.lock:
            jk, jk_mono = self.jk, self.jk_mono
            inv, inv_mono = self.inverter, self.inverter_mono
            holding, holding_mono = dict(self.holding), self.holding_mono

        def age(m):
            return None if m is None else round(now - m, 1)
        regs, info = self.emulator_registers()
        poll_age = self.slave.poll_age()
        return {
            'type': 'state', 'version': __version__, 'ts': round(time.time(), 1),
            'uptime_s': round(now - self.started_mono), 'simulate': self.c.simulate,
            'inverter': {'data': inv, 'age_s': age(inv_mono), **self.stats['inverter'].as_dict()},
            'holding': {'values': {str(k): v for k, v in sorted(holding.items())}, 'age_s': age(holding_mono),
                        'decoded': safety.decode_holding(holding), **self.stats['holding'].as_dict()},
            'jk': {'data': jk, 'age_s': age(jk_mono), **self.stats['jk'].as_dict()},
            'emulator': {
                'polling': poll_age is not None and poll_age < NOT_POLLED_AFTER_S,
                'poll_age_s': None if poll_age is None else round(poll_age, 1),
                'polls': self.slave.polls, 'unanswered': self.slave.unanswered,
                'crc_errors': self.slave.crc_errors, 'writes': self.slave.writes,
                'other_addrs': {f'0x{k:02x}': v for k, v in self.slave.other_addrs.items()},
                'regs': regs,
                'max_charge_a': info.get('max_chg_a') if regs else None,
                'max_discharge_a': info.get('max_dischg_a') if regs else None,
                'charge_voltage': info.get('chg_volt') if regs else None,
                'charge_ok': bool(regs) and info['charge_ok'],
                'discharge_ok': bool(regs) and info['discharge_ok'],
                'answering': regs is not None,
                'limit_reason': info.get('reason'),
                **self.stats['emulator'].as_dict(),
            },
            'ct': self.ct.state(now),
            'pi': self.pi,
            'safety': {'standby_assert_age_s': round(now - self.last_standby_assert),
                       'standby_watchdog_s': self.c.inverter.standby_watchdog_s,
                       'qc_max_arm_min': self.c.inverter.qc_max_arm_min,
                       'ct_calibration': self.c.inverter.ct_cal_slope != 0},
            'workers': {w.name: {'alive': w.is_alive(), 'age_s': round(w.age(), 1)} for w in self.workers},
            'clients': self.server.client_names(),
        }

    # -- supervision --------------------------------------------------------------------------------
    def check_workers(self) -> bool:
        healthy = True
        for w in self.workers:
            if w.critical and (not w.is_alive() or w.age() > w.max_age_s):
                log.critical('worker %s %s', w.name, 'died' if not w.is_alive() else f'stalled {w.age():.0f}s')
                healthy = False
        return healthy

    def check_transitions(self, snap: dict) -> None:
        emu = snap['emulator']
        if emu['limit_reason'] != self._last_reason:
            self._last_reason = emu['limit_reason']
            self.event('info', f"BMS emulator charge limit: {emu['limit_reason']} "
                               f"({emu['max_charge_a']} A @ {emu['charge_voltage']} V)")
        polling = emu['polling']
        if polling != self._polling and (polling or snap['uptime_s'] > NOT_POLLED_AFTER_S):
            if polling:
                self.event('info', 'inverter is polling the BMS emulator')
            else:
                self.event('warning', 'inverter is not polling the BMS emulator (battery type not Lithium / '
                                      'brand not 0:EG4, or battery-port cable)')
            self._polling = polling
        ct_error = (snap.get('ct') or {}).get('last_error')
        if ct_error != self._ct_error:
            if ct_error:
                self.event('warning', f'CT offset write failed: {ct_error}')
            elif self._ct_error is not None:
                self.event('info', 'CT offset writes are working again')
            self._ct_error = ct_error

    def run(self) -> int:
        self.server.start()
        for w in self.workers:
            w.start()
        sd_notify('READY=1')
        self.event('info', f'core {__version__} started' + (' (simulated devices)' if self.c.simulate else ''))
        last_file = -1e9
        code = 0
        try:
            while not self.stop.is_set():
                snap = self.snapshot()
                self.server.broadcast(snap)
                self.check_transitions(snap)
                if self.c.state_file and time.monotonic() - last_file >= 5:
                    try:
                        atomic_write_json(self.c.state_file, snap)
                    except OSError:
                        pass
                    last_file = time.monotonic()
                if self.check_workers():
                    sd_notify('WATCHDOG=1')
                else:
                    code = 1
                    break
                self.stop.wait(self.c.broadcast_s)
        finally:
            self.stop.set()
            self.wake.set()
            self.server.close()
            for w in self.workers:
                w.join(timeout=3)
        return code


def main(cfg: Config) -> int:
    core = Core(cfg)
    signal.signal(signal.SIGTERM, lambda *a: core.stop.set())
    signal.signal(signal.SIGINT, lambda *a: core.stop.set())
    return core.run()
