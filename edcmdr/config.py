"""Settings in config_dir()/settings.json: the shared sharing settings, plus the client's journal directories and integrations."""
from pathlib import Path

from edcmdr_core.settings import Settings as CoreSettings
from edcmdr_core.spec import DELAY_LABELS, DELAYS, SHAREABLE, SPEC  # noqa: F401

from . import paths


class Settings(CoreSettings):
    def __init__(self, path: Path | None = None):
        super().__init__(path)
        self.data.setdefault('journal_dirs', [])
        self.data.setdefault('start_paused', False)
        if not self.data['journal_dirs']:
            self.data['journal_dirs'] = [str(p) for p in paths.default_journal_dirs()]

    @property
    def journal_dirs(self) -> list[Path]:
        return [Path(p) for p in self.data['journal_dirs']]

    def set_journal_dirs(self, dirs: list[str]) -> None:
        with self.lock:
            self.data['journal_dirs'] = list(dict.fromkeys(dirs))
            self.save()

    def set_integration(self, fid: str, service: str, values: dict) -> None:
        with self.lock:
            c = self.data['commanders'].setdefault(fid, {})
            c.setdefault('integrations', {})[service] = dict(values)
            self.save()
