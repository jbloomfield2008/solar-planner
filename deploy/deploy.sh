#!/bin/bash
# Build a release from the working tree, upload it, run the tests on the Pi, activate it and
# restart only what changed.  The core (BMS bridge) is restarted only when core code changed.
#
#   deploy/deploy.sh                 normal deploy
#   deploy/deploy.sh --restart-core  force a core restart
#   deploy/deploy.sh --keep-core     never restart the core (e.g. only hub settings in config.py changed)
#   deploy/deploy.sh --cutover       first install: switch over from the legacy scripts
#
# PI=root@192.168.0.162 by default.
set -euo pipefail
PI=${PI:-root@192.168.0.162}
cd "$(dirname "$0")/.."
SHA=$(git rev-parse --short HEAD 2>/dev/null || echo nogit)
DIRTY=$(git diff --quiet HEAD 2>/dev/null || echo -dirty)
ID=$(date +%Y%m%d-%H%M%S)-$SHA$DIRTY
mkdir -p build
tar czf "build/$ID.tgz" --exclude=__pycache__ --exclude='*.pyc' solar01 web deploy tests README.md
echo "release $ID ($(du -h "build/$ID.tgz" | cut -f1))"
scp -q "build/$ID.tgz" "$PI:/tmp/$ID.tgz"
tr -d '\r' < deploy/remote_activate.sh | ssh "$PI" "bash -s -- $ID $*"
