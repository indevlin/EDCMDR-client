#!/usr/bin/env python3
"""One-file build with PyInstaller. The build key is injected for the build and removed afterwards.

Key source, first found: $EDCMDR_BUILD_KEY, ~/.config/edcmdr/build.key.
Without one the build is unofficial: it runs, but EDCMDR refuses its pushes.

  python3 scripts/build.py              local build
  python3 scripts/build.py --release    refuses to build without a build key and server keys
"""
import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = ROOT / 'edcmdr' / '_build_secret.py'
QT_EXCLUDES = ['QtNetwork', 'QtQml', 'QtQuick', 'QtSql', 'QtTest', 'QtXml', 'QtOpenGL', 'QtPdf', 'QtSvg',
               'QtWebEngineCore', 'QtMultimedia', 'QtDBus', 'QtConcurrent', 'QtPrintSupport', 'QtDesigner']


def build_key() -> str | None:
    env = os.environ.get('EDCMDR_BUILD_KEY', '').strip()
    if env:
        return env
    f = Path.home() / '.config' / 'edcmdr' / 'build.key'
    return f.read_text().strip() if f.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--release', action='store_true')
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    from edcmdr import VERSION, keys

    key = build_key()
    if args.release and not key:
        sys.exit('release build needs EDCMDR_BUILD_KEY')
    if args.release and not keys.SERVER_KEYS:
        sys.exit('release build needs SERVER_KEYS in edcmdr/keys.py')
    tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', str(SECRET)], cwd=ROOT, capture_output=True)
    if tracked.returncode == 0:
        sys.exit(f'{SECRET} is tracked by git; remove it from the index first')

    sep = ';' if os.name == 'nt' else ':'
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
           '--name', f'EDCMDR-{VERSION}-{platform.system().lower()}-{platform.machine().lower()}',
           '--add-data', f'protocol/v1/spec.json{sep}protocol/v1',
           '--add-data', f'edcmdr/data{sep}edcmdr/data',
           '--add-data', f'LICENSE{sep}.']
    for m in QT_EXCLUDES:
        cmd += ['--exclude-module', f'PySide6.{m}']
    cmd.append('edcmdr_client.py')
    try:
        if key:
            SECRET.write_text(f"BUILD_KEY = '{key}'\n")
        else:
            print('warning: no build key; this build cannot publish to EDCMDR', file=sys.stderr)
        subprocess.run(cmd, cwd=ROOT, check=True)
    finally:
        SECRET.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
