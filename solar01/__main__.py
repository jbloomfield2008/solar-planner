"""solar01 command line: ``python3 -m solar01 [--config PATH] core|hub|check-config|version``."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, config
from .util import setup_logging

DEFAULT_CONFIG = '/etc/solar01/config.toml'


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='solar01', description=__doc__)
    p.add_argument('--config', default=os.environ.get('SOLAR01_CONFIG'), help=f'TOML config (default {DEFAULT_CONFIG})')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('core', help='serial I/O, BMS emulator, write safety (no network)')
    sub.add_parser('hub', help='history, planner, MQTT bridge, web UI')
    sub.add_parser('check-config', help='print the effective configuration')
    sub.add_parser('version')
    args = p.parse_args(argv)
    if args.cmd == 'version':
        print(__version__)
        return 0
    path = args.config
    if path is None and os.path.exists(DEFAULT_CONFIG):
        path = DEFAULT_CONFIG
    try:
        cfg = config.load(path)
    except (OSError, ValueError) as e:
        print(f'config error: {e}', file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)
    if args.cmd == 'check-config':
        print(json.dumps(config.redacted(cfg), indent=2))
        return 0
    if args.cmd == 'core':
        from .core.service import main as run
    else:
        from .hub.service import main as run
    return run(cfg)


if __name__ == '__main__':
    sys.exit(main())
