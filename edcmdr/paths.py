"""Per-OS locations: default journal directories, bundled data. The config directory is edcmdr_core's."""
import os
import sys
from pathlib import Path

from edcmdr_core.paths import config_dir  # noqa: F401


def default_journal_dirs() -> list[Path]:
    home = Path.home()
    rel = Path('Saved Games') / 'Frontier Developments' / 'Elite Dangerous'
    if sys.platform == 'win32':
        cands = [Path(os.environ.get('USERPROFILE', home)) / rel]
    else:
        steam = [home / '.local/share/Steam', home / '.steam/steam',
                 home / '.var/app/com.valvesoftware.Steam/.local/share/Steam']
        cands = [s / 'steamapps/compatdata/359320/pfx/drive_c/users/steamuser' / rel for s in steam]
    return [c for c in cands if c.is_dir()]


def data_dir() -> Path:
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    return base / 'edcmdr' / 'data' if (base / 'edcmdr' / 'data').is_dir() else Path(__file__).resolve().parent / 'data'

