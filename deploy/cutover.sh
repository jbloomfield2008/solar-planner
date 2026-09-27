#!/bin/bash
# One-time switch from the legacy scripts (solar-mqtt, bms-emu, solar-tou) to solar01.
# Rolls back to the legacy services automatically if solar01 does not come up healthy.
#   cutover.sh <release dir>      (run as root; normally via deploy.sh --cutover)
set -uo pipefail
REL=${1:?usage: cutover.sh <release dir>}
BASE=/opt/solar01

rollback() {
  echo "== ROLLING BACK to the legacy services"
  systemctl disable --now solar01-hub solar01-core 2>/dev/null
  systemctl enable --now solar-mqtt bms-emu solar-tou
  systemctl is-active solar-mqtt bms-emu solar-tou
}
fail() {
  echo "CUTOVER FAILED: $*"
  journalctl -u solar01-core -u solar01-hub --since '-3min' --no-pager | tail -40
  rollback
  exit 1
}

echo "== preflight"
python3 -c 'import aiohttp, serial, paho.mqtt.client, tomllib' || { echo "missing python packages"; exit 1; }
for p in 1.1 1.2 1.4; do
  ls /dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:$p:1.0-port0 >/dev/null || { echo "missing USB serial adapter at port $p"; exit 1; }
done
id solar01 >/dev/null 2>&1 || useradd --system --home-dir /var/lib/solar01 --no-create-home --shell /usr/sbin/nologin solar01
install -d -m 0750 -o root -g solar01 /etc/solar01
if [ ! -f /etc/solar01/config.toml ]; then
  PYTHONPATH="$REL" python3 "$REL/deploy/migrate_env.py" /etc/solar-mqtt.env > /etc/solar01/config.toml.new || { echo "config migration failed"; exit 1; }
  mv /etc/solar01/config.toml.new /etc/solar01/config.toml
  echo "created /etc/solar01/config.toml from /etc/solar-mqtt.env:"
  sed 's/^password = .*/password = "***"/' /etc/solar01/config.toml
fi
chown root:solar01 /etc/solar01/config.toml
chmod 0640 /etc/solar01/config.toml
PYTHONPATH="$REL" python3 -m solar01 --config /etc/solar01/config.toml check-config > /dev/null || { echo "config invalid"; exit 1; }
ln -sfn "$REL" $BASE/current.new && mv -T $BASE/current.new $BASE/current
install -m 0644 "$REL/deploy/solar01-core.service" "$REL/deploy/solar01-hub.service" /etc/systemd/system/
systemctl daemon-reload

echo "== stopping the legacy services"
systemctl disable --now solar-tou bms-emu solar-mqtt
rm -f /run/solar01/core-state.json
echo "== starting solar01"
systemctl enable --now solar01-core || fail "core did not start"
systemctl enable --now solar01-hub || fail "hub did not start"

echo "== verifying the core (fresh inverter + JK data, emulator answering)"
ok=
for i in $(seq 1 45); do
  if python3 - <<'EOF' 2>/dev/null
import json, sys
d = json.load(open('/run/solar01/core-state.json'))
inv, jk, emu = d['inverter'], d['jk'], d['emulator']
fresh = lambda s: s['age_s'] is not None and s['age_s'] < 15
sys.exit(0 if fresh(inv) and fresh(jk) and emu['answering'] else 1)
EOF
  then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || fail "core did not deliver fresh inverter and JK data within 90 s"
python3 - <<'EOF'
import json
d = json.load(open('/run/solar01/core-state.json'))
inv, jk, emu, dec = d['inverter']['data'], d['jk']['data'], d['emulator'], d['holding'].get('decoded', {})
print(f"inverter: {inv['mode']}, SOC {inv['soc']} %, PV {inv['pv_power']} W, load {inv['load_power']} W")
print(f"JK BMS:   SOC {jk['soc']} %, {jk['voltage']} V, {jk['current']} A, cells {jk['cell_voltage_min']}-{jk['cell_voltage_max']} mV")
print(f"emulator: answering={emu['answering']} polling={emu['polling']} charge {emu['max_charge_a']} A ({emu['limit_reason']})")
print(f"inverter battery type: {dec.get('battery_type')} ({dec.get('h0')})")
EOF

echo "== verifying the hub"
ok=
for i in $(seq 1 30); do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1/healthz', timeout=3)" 2>/dev/null; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || fail "hub /healthz not OK within 60 s"
echo "CUTOVER COMPLETE: web UI at http://$(hostname -I | awk '{print $1}')/"
echo "rollback if ever needed: systemctl disable --now solar01-hub solar01-core && systemctl enable --now solar-mqtt bms-emu solar-tou"
