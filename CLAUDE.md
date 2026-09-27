# solar01

Home solar controller on a Raspberry Pi. It does three jobs:

1. **BMS bridge**: reads the JK BMS over RS485 and emulates a Luxpower/EG4 BMS on the EG4 FlexBoss21 inverter's
   battery port, so the inverter runs closed loop.
2. **Charge planner**: for the SDG&E time-of-use plan, decides standby holds and grid quick-charges, only ever
   inside super off-peak windows. It acts only when the forecast would take the battery below the 15 % floor
   before the next window, and sizes the charge to keep a 20 % reserve. Outside the windows, a battery at the
   floor goes into standby (the grid carries the house) until the next window. Also runs **manual charges**
   (to a target SOC, or for a set time) started from the console.
3. **CT calibration**: adjusts the inverter's CT offset register (H119) from the house load.

A local web console (port 80) also shows headline numbers: solar forecast and actual, home use, SOC and its
change over 24 h, grid import today and this billing cycle, cycle cost and savings. The tariff (TOU rates plus
usage-tier adders) and the billing-cycle start day are set in a dialog in the console.

README.md is the full design document (architecture, safety rules, register notes, operations). Read it before
changing anything that writes to the inverter.

## Where it runs

| | |
|---|---|
| Host | `solar01`, `root@192.168.0.162` (Ubuntu, aarch64, Python 3.14 from apt; no pip) |
| Code | `/opt/solar01/releases/<timestamp>-<sha>`; `/opt/solar01/current` is a symlink to the active release |
| Config | `/etc/solar01/config.toml` (root:solar01 0640, has the MQTT password). Defaults live in `solar01/config.py` |
| Data | `/var/lib/solar01/solar01.db` (SQLite, WAL): hourly energy, minute averages, events, kv settings |
| Services | `solar01-core` (root, serial I/O, no network) and `solar01-hub` (user solar01: planner, web, MQTT) |
| Console | http://192.168.0.162/ with a JSON API under `/api/` (listed at the top of `solar01/hub/web.py`) |
| Home Assistant | MQTT broker at 192.168.0.97 (optional; the Pi must never depend on it) |
| Remote | https://github.com/jbloomfield2008/solar-planner |

## Layout

```
solar01/core/        core process: serial threads, BMS emulator, write safety (safety.py), CT calibration
solar01/devices/     protocol code: flexboss.py (Modbus registers), jkbms.py, bmsemu.py, sim.py (fake devices)
solar01/hub/         hub process: service.py (wiring), state.py, history.py (SQLite), web.py (API), mqtt.py
solar01/hub/planner/ model.py (pure SOC simulation + decide), planner.py (inputs, actuation, manual charge),
                     calendar.py (TOU windows, holidays), weather.py (Open-Meteo PV forecast)
solar01/hub/tariff.py   TOU rates, usage tiers, billing cycle (pure)
solar01/hub/summary.py  headline numbers for /api/summary
web/                 static console: app.js (Preact + htm, no build step), styles.css, vendored libs and fonts
deploy/              deploy.sh (run from the workstation), remote_activate.sh, systemd units, example config
tests/               unittest suite; runs without hardware, also run on the Pi by every deploy
```

## Working on it

- Tests: `python -m unittest discover -s tests -t .` (Python 3.14 and aiohttp are on the workstation).
- Run locally with simulated devices: see README "Development". To try the console on real history, copy the
  live database first (read-only backup, safe while the hub runs):
  `ssh root@192.168.0.162 'python3 -c "import sqlite3; s=sqlite3.connect(\"file:/var/lib/solar01/solar01.db?mode=ro\", uri=True); d=sqlite3.connect(\"/tmp/c.db\"); s.backup(d)"' && scp root@192.168.0.162:/tmp/c.db build/dev.db`
- Deploy (Git Bash): `deploy/deploy.sh`. It uploads a tarball, runs the tests on the Pi, switches the
  `current` symlink and restarts the hub. The core is restarted automatically when core code, `ipc.py`,
  `util.py`, `__main__.py` **or `config.py`** changed. Restarting the core briefly interrupts the BMS bridge,
  so pass `--keep-core` when a `config.py` change only touches hub settings.
- Roll back: point `/opt/solar01/current` at the previous release and restart both services (README, Operations).

## Rules

- The core refuses any inverter write outside `solar01/core/safety.py` (H21 bit 9, H233 bit 0, H234 5..60 min).
  Never widen those rules to make a hub feature work; build the feature from the allowed writes (as manual
  charge does, by re-arming the quick-charge countdown).
- Nothing in the core may depend on the network, the hub or Home Assistant.
- The hub cannot write `/etc/solar01`. Settings edited in the console (planner on/off, tariff, manual charge)
  live in the database `kv` table.
- The Pi has no RTC: use `time.monotonic()` for ages in the core; the hub checks `clock_synced()` before
  recording history.
- Keep the console usable offline: vendor any library into `web/vendor`, no CDNs.
