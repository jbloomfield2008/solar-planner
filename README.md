# solar01

One application on the Raspberry Pi `solar01` that runs the EG4 FlexBoss21 inverter, the JK BMS
bridge, the SDG&E time-of-use charge planner, the Home Assistant MQTT bridge and a local web
console. It replaces three scripts (`solar_mqtt.py`, `bms_emu.py`, `solar_tou.py`) that were
glued together through Home Assistant's MQTT broker.

## Why it was rebuilt

On 2026-09-13 Home Assistant went down hard. The BMS emulator received its JK data through the
broker, so it went stale, stopped answering the inverter, and then crash-looped on a blocking
broker connect. The inverter lost its battery communication. Nothing on the Pi should depend on
Home Assistant, and the system should be observable locally when HA is down.

## Architecture

```
                 +------------------------- solar01-core (root, no network) -------------------------+
 JK BMS  --RS485-|--> jk thread ----latest sample----> emulator thread --RS485--> inverter battery port |
 inverter meter -|--> inverter thread: poll 5 s, CT calibration, safety-checked writes, standby watchdog |
 port (RS485)    |    pi thread: undervoltage, temperature                                                |
                 |    main loop: snapshot every 2 s -> /run/solar01/core.sock + core-state.json,          |
                 |               systemd watchdog only while the critical threads make progress        |
                 +--------------------------------------------|-------------------------------------------+
                                                              | JSON lines over a unix socket
                 +------------------------- solar01-hub (user solar01) ------------|------------------------+
                 |  store + health  <-- core link (reconnects)                                              |
                 |  history (SQLite: hourly energy, minute averages, events)                              |
                 |  planner (TOU decisions, writes go back to core)                                        |
                 |  MQTT bridge to Home Assistant (optional, same entities as before)                      |
                 |  web UI + JSON API on port 80                                                           |
                 +-----------------------------------------------------------------------------------------+
```

* **The BMS bridge needs only the core process and the USB adapters.** No network, no broker,
  no hub. JK sample ages use `time.monotonic()` because the Pi has no RTC and its clock steps at boot.
* **Anything that talks to the network lives in the hub.** It can crash, restart or lose the LAN
  without touching the bridge. Each hub part (core link, planner, MQTT, web, housekeeping) is a
  supervised task restarted with back-off if it fails.
* **Both are systemd `Type=notify` services with watchdogs.** The core is restarted if a critical
  thread dies or stalls; the hub if its event loop blocks. `Conflicts=` keeps the legacy services
  from ever running alongside (they own the same serial ports).
* **Python 3 from Ubuntu's archive** (pyserial, paho-mqtt, aiohttp; the Pi has no pip). The
  protocol code and planner were ported with their tests rather than rewritten in another
  language; the timing risk of Python is removed by isolating the bridge in its own process.
* **The web UI is static files** (Preact + htm and uPlot vendored, Barlow fonts vendored): it works
  with no internet.

## Safety rules enforced by the core

Whatever the hub, a bug or a LAN client asks for:

| Register | Rule |
|---|---|
| H21 | only bit 9 (standby) may change; the AC Charge bit (bit 7) is never touched |
| H233 | only bit 0 (quick charge) may change |
| H234 | quick-charge countdown 5..`qc_max_arm_min` (60) minutes; the firmware ends the charge if the hub dies |
| H119 | written only by the core's own CT calibration (never positive: no export bias) |
| anything else | refused |

* **Standby watchdog:** if the inverter is in standby and the planner has not asserted a hold for
  20 minutes, the core sets it back to normal.
* **Emulator fail-safe:** BMS data older than 30 s forbids charging; older than 300 s stops answering
  so the inverter sees a BMS communication loss.
* **MQTT writes removed:** the holding-register write service no longer exists.
* **CT calibration** lives in the core (`solar01/core/ct.py`): offset = slope x load + intercept, clamped, written
  when the target moves by the hysteresis and at most once per interval. The console's CT calibration panel shows
  the offset, the target at the current load, the calibration line with the no-write band, the offsets used over
  the last 24 hours and every write since the core started.

## Operations

| Task | Command (on the Pi) |
|---|---|
| status | `systemctl status solar01-core solar01-hub` |
| logs | `journalctl -u solar01-core -u solar01-hub -f` |
| core snapshot | `python3 -m json.tool /run/solar01/core-state.json` |
| effective config | `PYTHONPATH=/opt/solar01/current python3 -m solar01 --config /etc/solar01/config.toml check-config` |
| restart hub only | `systemctl restart solar01-hub` (the bridge keeps running) |
| edit the database by hand | `systemctl stop solar01-hub`, then `sqlite3 /var/lib/solar01/solar01.db`, then start it again (the hub holds a write transaction between its 5-minute commits) |
| roll back to the scripts | `systemctl disable --now solar01-hub solar01-core && systemctl enable --now solar-mqtt bms-emu solar-tou` |
| previous release | `ln -sfn /opt/solar01/releases/<id> /opt/solar01/current && systemctl restart solar01-hub solar01-core` |

Web console: `http://192.168.0.162/`. API: `/api/state`, `/api/stream` (server-sent events),
`/api/history?hours=24`, `/api/daily?days=14`, `/api/events`, `POST /api/planner {"enabled": false}`,
`/api/config`, `/healthz`.

### Deploying (from the workstation, Git Bash)

```
deploy/deploy.sh                  # upload, run the tests on the Pi, activate, restart the hub
deploy/deploy.sh --restart-core   # also restart the core (done automatically when core code changed)
deploy/deploy.sh --keep-core      # never restart the core, e.g. when only hub settings in config.py changed
deploy/deploy.sh --cutover        # first install: stop the legacy scripts, start solar01, roll back on failure
```

Releases live in `/opt/solar01/releases/<timestamp>-<git sha>`, `/opt/solar01/current` points at the
active one, the last five are kept. Configuration is `/etc/solar01/config.toml` (see
`deploy/config.example.toml`); the cutover generates it from `/etc/solar-mqtt.env`.

### Development

```
python -m unittest discover -s tests -t .          # full test suite, no hardware needed
python tools/seed_dev_db.py build/dev.db           # a week of fake history
python -m solar01 --config build/dev.toml core     # simulated inverter + BMS
python -m solar01 --config build/dev.toml hub      # web UI on http://127.0.0.1:8088/
```

## Home Assistant

The bridge publishes the same state topics, discovery topics and `unique_id`s as the old scripts, so
entities, history and the "Solar & Battery" dashboard continue. Additions: `SOC source`,
`Battery charge cap`, `Inverter battery type`, `Core process connected`, `Inverter polling BMS emulator`.
One MQTT connection carries all devices; the bridge and planner entities also watch
`solar/bmsemu/availability` and `solar/tou/availability`.

## What we learned (kept in code comments and tests)

**Inverter meter port** - Modbus RTU 19200 8N1, slave 1, never polled faster than every 5 s.
Input blocks of 40 at 0/40/125/165/205. I172/173 = total load energy (U32, 0.1 kWh); no per-leg grid
power exists. I210 = quick-charge seconds left. H0 high byte: bits 0-1 battery type (1 lead-acid,
2 lithium), bits 2-6 lithium brand (0 = EG4, required for the closed loop; inferred from 0x8100 before
and 0x8200 after the 2026-09-01 switch). H21 bit 9 = normal/standby (standby: grid bypass feeds the
house, PV and battery idle, waking takes 5-9 min); bit 7 = AC Charge, not used because loads stay on
grid while it is enabled. H233 bit 0 + H234 = quick charge (H234 is rejected while inactive, enabling
starts a 60-minute default). H119 CT offset LSB is 0.1 W (the 18kPV document says 1 W), clamped
+5000/-2500. H60 = 56 % output cap. H66 = AC charge power (0.1 kW).

**Battery port** - the inverter polls `01 03 00 00 10 00 48 0A` every 500 ms at 9600 baud and expects
16 little-endian registers in the Luxpower layout documented in `solar01/devices/bmsemu.py`. A status
word of 0 shows "Forbidden" on the LCD. It polls only when the battery type is Lithium, brand 0.

**JK BMS** - classic "NW" protocol at 115200; current sign bit set = charging; temperatures above 100
are negative. Frames with the wrong cell count or implausible values are dropped because the emulator
forwards them to the inverter.

**Planner** - see the docstring in `solar01/hub/planner/model.py`. Holds and grid charges are only used
to close a forecast shortfall and are scheduled as late in the window as possible; every hold and charge
ends 10 min before the window closes, because the inverter takes about 5 minutes to return to normal after
standby or a quick charge; the projection covers 24 hours, previewing what later windows would decide; SOC comes from the BMS; the charge rate is capped by H101 and the emulator's
limit. Margins set by the user: load 1.05, PV 0.9.

**Pi** - undervoltage history (throttled flags); no RTC; `/dev/vchiq` is root-only on this image.
