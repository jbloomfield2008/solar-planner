#!/bin/bash
# Runs on the Pi (fed by deploy.sh): unpack, test, activate, restart what changed, verify.
set -euo pipefail
ID=$1
shift
RESTART_CORE=auto
CUTOVER=no
for arg in "$@"; do
  case $arg in
    --restart-core) RESTART_CORE=yes ;;
    --keep-core) RESTART_CORE=no ;;       # e.g. only hub settings in config.py changed; the bridge stays up
    --cutover) CUTOVER=yes ;;
  esac
done
BASE=/opt/solar01
REL=$BASE/releases/$ID
install -d -m 0755 $BASE/releases
mkdir -p "$REL"
tar xzf "/tmp/$ID.tgz" -C "$REL"
rm -f "/tmp/$ID.tgz"
sed -i 's/\r$//' "$REL"/deploy/*.sh "$REL"/deploy/*.service
echo "== tests on the Pi"
if ! (cd "$REL" && python3 -W ignore -m unittest discover -s tests -t . > "$REL/test.log" 2>&1); then
  tail -30 "$REL/test.log"
  echo "TESTS FAILED - release $ID left inactive"
  exit 1
fi
grep -E '^(Ran|OK)' "$REL/test.log"
python3 -m compileall -q "$REL/solar01"

if [ "$CUTOVER" = yes ]; then
  exec bash "$REL/deploy/cutover.sh" "$REL"
fi
if [ ! -f /etc/systemd/system/solar01-core.service ]; then
  echo "solar01 is not installed yet: run deploy.sh --cutover"
  exit 1
fi

CUR=$(readlink -f $BASE/current || true)
core_changed=yes
if [ -n "$CUR" ] && [ "$RESTART_CORE" = auto ]; then
  if diff -rq -x __pycache__ "$CUR/solar01/core" "$REL/solar01/core" >/dev/null &&
     diff -rq -x __pycache__ "$CUR/solar01/devices" "$REL/solar01/devices" >/dev/null &&
     cmp -s "$CUR/solar01/ipc.py" "$REL/solar01/ipc.py" && cmp -s "$CUR/solar01/config.py" "$REL/solar01/config.py" &&
     cmp -s "$CUR/solar01/util.py" "$REL/solar01/util.py" && cmp -s "$CUR/solar01/__main__.py" "$REL/solar01/__main__.py" &&
     cmp -s "$CUR/deploy/solar01-core.service" "$REL/deploy/solar01-core.service"; then
    core_changed=no
  fi
fi
[ "$RESTART_CORE" = yes ] && core_changed=yes
[ "$RESTART_CORE" = no ] && core_changed=no

ln -sfn "$REL" $BASE/current.new
mv -T $BASE/current.new $BASE/current
for unit in solar01-core.service solar01-hub.service; do
  cmp -s "$REL/deploy/$unit" "/etc/systemd/system/$unit" || install -m 0644 "$REL/deploy/$unit" /etc/systemd/system/
done
systemctl daemon-reload
if [ "$core_changed" = yes ]; then
  echo "== restarting core (core code changed)"
  systemctl restart solar01-core
else
  echo "== core unchanged, not restarted"
fi
systemctl restart solar01-hub
echo "== waiting for health"
for i in $(seq 1 30); do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1/healthz', timeout=3)" 2>/dev/null; then
    echo "healthy: release $ID active"
    ls -1dt $BASE/releases/* | tail -n +6 | xargs -r rm -rf
    exit 0
  fi
  sleep 2
done
echo "WARNING: /healthz not OK after 60 s"
python3 -c "import urllib.request,json
try:
    urllib.request.urlopen('http://127.0.0.1/healthz', timeout=3)
except Exception as e:
    print(getattr(e, 'read', lambda: str(e).encode())().decode()[:2000])"
journalctl -u solar01-hub -u solar01-core --since '-2min' --no-pager | tail -20
exit 1
