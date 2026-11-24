#!/usr/bin/env python3
"""One-file build with PyInstaller. The build key is injected for the build and removed afterwards.

Key source, first found: $EDCMDR_BUILD_KEY, ~/.config/edcmdr/build.key.
$INARA_APP_KEY, when set, goes into the build with it.
Without one the build is unofficial: it runs, but EDCMDR refuses its pushes.

  python3 scripts/build.py              local build
  python3 scripts/build.py --release    refuses to build without a build key and server keys
"""
import argparse
import os
import time
import re
import shutil
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INFO = ROOT / 'edcmdr' / '_build_info.py'
SECRET = ROOT / 'edcmdr' / '_build_secret.py'
QT_EXCLUDES = ['QtNetwork', 'QtQml', 'QtQuick', 'QtSql', 'QtTest', 'QtXml', 'QtOpenGL', 'QtPdf', 'QtSvg',
               'QtWebEngineCore', 'QtMultimedia', 'QtDBus', 'QtConcurrent', 'QtPrintSupport', 'QtDesigner']


OS = {'Linux': 'linux', 'Windows': 'windows', 'Darwin': 'macos'}

def build_key() -> str | None:
    env = os.environ.get('EDCMDR_BUILD_KEY', '').strip()
    if env:
        return env
    f = Path.home() / '.config' / 'edcmdr' / 'build.key'
    return f.read_text().strip() if f.exists() else None


def build_version() -> str:
    """$EDCMDR_VERSION, else a YYYYMMDD tag on HEAD, else the version file, else today (UTC)."""
    v = os.environ.get('EDCMDR_VERSION', '')
    if v:
        if not re.fullmatch(r'\d{8}', v):
            sys.exit(f'EDCMDR_VERSION {v!r} is not YYYYMMDD')
        return v
    tags = subprocess.run(['git', 'tag', '--points-at', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.split()
    dated = sorted(t for t in tags if re.fullmatch(r'\d{8}', t))
    if dated:
        return dated[-1]
    f = ROOT / 'version'
    v = f.read_text().strip() if f.exists() else ''
    return v if re.fullmatch(r'\d{8}', v) else time.strftime('%Y%m%d', time.gmtime())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--release', action='store_true')
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    from edcmdr import keys
    version = build_version()

    key = build_key()
    if args.release and not key:
        sys.exit('release build needs EDCMDR_BUILD_KEY')
    if args.release and not keys.SERVER_KEYS:
        sys.exit('release build needs SERVER_KEYS in edcmdr/keys.py')
    tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', str(SECRET)], cwd=ROOT, capture_output=True)
    if tracked.returncode == 0:
        sys.exit(f'{SECRET} is tracked by git; remove it from the index first')

    sep = ';' if os.name == 'nt' else ':'
    # macOS gets an .app bundle, which is a folder: one-file mode makes no sense there.
    mode = '--onedir' if platform.system() == 'Darwin' else '--onefile'
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', mode, '--windowed',
           '--name', f'EDCMDR-{version}-{OS.get(platform.system(), platform.system().lower())}',
           '--add-data', f'protocol/v1/spec.json{sep}protocol/v1',
           '--add-data', f'edcmdr/data{sep}edcmdr/data',
           '--add-data', f'LICENSE{sep}.']
    for m in QT_EXCLUDES:
        cmd += ['--exclude-module', f'PySide6.{m}']
    cmd.append('edcmdr_client.py')
    for old in (ROOT / 'dist').glob('EDCMDR-*'):
        # dist/ holds this build only: earlier builds, under any name, go first.
        shutil.rmtree(old) if old.is_dir() else old.unlink()
    try:
        INFO.write_text(f"VERSION = '{version}'\n")
        print(f'building version {version}')
        if key:
            inara = os.environ.get('INARA_APP_KEY', '').strip()
            SECRET.write_text(f"BUILD_KEY = '{key}'\nINARA_APP_KEY = {inara!r}\n")
        else:
            print('warning: no build key; this build cannot publish to EDCMDR', file=sys.stderr)
        subprocess.run(cmd, cwd=ROOT, check=True)
    finally:
        SECRET.unlink(missing_ok=True)
        INFO.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
