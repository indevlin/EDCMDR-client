#!/usr/bin/env python3
"""EDCMDR client entry point.

  edcmdr_client.py               tray app
  edcmdr_client.py --headless    no window: read, push, log to stderr
  edcmdr_client.py --once        one read-and-push pass, then exit (tests, cron)
  edcmdr_client.py --list        commanders found in the journal directories
"""
import argparse
import logging
import sys

from edcmdr import NAME, VERSION
from edcmdr.config import Settings


def main() -> int:
    ap = argparse.ArgumentParser(prog='edcmdr')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--settings', action='store_true', help='open the settings window at start')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--journal', action='append', help='journal directory (repeatable; replaces saved ones for this run)')
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--version', action='version', version=f'{NAME} {VERSION}')
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    settings = Settings()
    if args.journal:
        settings.data['journal_dirs'] = args.journal

    if args.list:
        from edcmdr import journal
        w = journal.Watcher(settings.journal_dirs, lambda f, e: None, frozenset())
        w.poll()
        for fid, name in sorted(w.commanders().items(), key=lambda kv: kv[1]):
            print(f'{name}')
        return 0

    from edcmdr.uplink import Engine
    if args.once or args.headless:
        engine = Engine(settings)
        if args.once:
            engine.tick()
            for c in engine.snapshot()['commanders']:
                print(f"{c['name']}: {'enrolled' if c['enrolled'] else 'not enrolled'}"
                      f"{' — ' + c['blocked'] if c['blocked'] else ''}{' — ' + c['error'] if c['error'] else ''}")
            print(engine.status)
            return 0 if engine.status == 'Connected' else 1
        try:
            engine.run()
        except KeyboardInterrupt:
            pass
        return 0

    from edcmdr.ui.app import run
    shared = any(any((c.get('prefs') or {}).get('share', {}).values()) for c in settings.data.get('commanders', {}).values())
    return run(settings, show_settings=args.settings or not shared or not settings.journal_dirs)


if __name__ == '__main__':
    sys.exit(main())
